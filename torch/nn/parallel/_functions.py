import warnings

import torch
import types
import functools
import importlib
from . import comm
from torch.autograd import Function
from torch._utils import _get_device_index, _get_available_device_type
from typing import List, Optional, Tuple

@functools.cache
def _get_device_impl() -> Tuple[str, types.ModuleType]:
    ddp_device_type = _get_available_device_type()
    if ddp_device_type is not None:
        ddp_gpu = importlib.import_module("torch." + ddp_device_type)
    else:
        raise ImportError("Failed to import the gpu device module of pytorch (torch.cuda or torch.xpu).")
    return (ddp_device_type, ddp_gpu)

class Broadcast(Function):

    @staticmethod
    def forward(ctx, target_gpus, *inputs):
        assert all(i.device.type != 'cpu' for i in inputs), (
            'Broadcast function not implemented for CPU tensors'
        )
        target_gpus = [_get_device_index(x, True) for x in target_gpus]
        ctx.target_gpus = target_gpus
        if len(inputs) == 0:
            return tuple()
        ctx.num_inputs = len(inputs)
        ctx.input_device = inputs[0].get_device()
        outputs = comm.broadcast_coalesced(inputs, ctx.target_gpus)
        non_differentiables = []
        for idx, input_requires_grad in enumerate(ctx.needs_input_grad[1:]):
            if not input_requires_grad:
                for output in outputs:
                    non_differentiables.append(output[idx])
        ctx.mark_non_differentiable(*non_differentiables)
        return tuple([t for tensors in outputs for t in tensors])

    @staticmethod
    def backward(ctx, *grad_outputs):
        return (None,) + ReduceAddCoalesced.apply(ctx.input_device, ctx.num_inputs, *grad_outputs)


class ReduceAddCoalesced(Function):

    @staticmethod
    def forward(ctx, destination, num_inputs, *grads):
        ctx.target_gpus = [grads[i].get_device() for i in range(0, len(grads), num_inputs)]

        grads_ = [grads[i:i + num_inputs]
                  for i in range(0, len(grads), num_inputs)]
        return comm.reduce_add_coalesced(grads_, destination)

    @staticmethod
    def backward(ctx, *grad_outputs):
        return (None, None,) + Broadcast.apply(ctx.target_gpus, *grad_outputs)


class Gather(Function):

    @staticmethod
    def forward(ctx, target_device, dim, *inputs):
        assert all(i.device.type != 'cpu' for i in inputs), (
            'Gather function not implemented for CPU tensors'
        )
        if (target_device == 'cpu'):
            ctx.target_device = 'cpu'
        else:
            target_device = _get_device_index(target_device, True)
            ctx.target_device = target_device
        ctx.dim = dim
        ctx.input_gpus = tuple(i.get_device() for i in inputs)
        if all(t.dim() == 0 for t in inputs) and dim == 0:
            inputs = tuple(t.view(1) for t in inputs)
            warnings.warn('Was asked to gather along dimension 0, but all '
                          'input tensors were scalars; will instead unsqueeze '
                          'and return a vector.')
            ctx.unsqueezed_scalar = True
        else:
            ctx.unsqueezed_scalar = False
        ctx.input_sizes = tuple(i.size(ctx.dim) for i in inputs)
        return comm.gather(inputs, ctx.dim, ctx.target_device)

    @staticmethod
    def backward(ctx, grad_output):
        scattered_grads = Scatter.apply(ctx.input_gpus, ctx.input_sizes, ctx.dim, grad_output)
        if ctx.unsqueezed_scalar:
            scattered_grads = tuple(g[0] for g in scattered_grads)
        return (None, None) + scattered_grads


class Scatter(Function):

    @staticmethod
    def forward(ctx, target_gpus, chunk_sizes, dim, input):
        target_gpus = [_get_device_index(x, True) for x in target_gpus]
        ctx.dim = dim
        ctx.input_device = input.get_device() if input.device.type != "cpu" else -1
        streams = None
        ddp_gpu = _get_device_impl()[1]
        if ddp_gpu.is_available() and ctx.input_device == -1:
            # Perform CPU to GPU copies in a background stream
            streams = [_get_stream(device) for device in target_gpus]
        outputs = comm.scatter(input, target_gpus, chunk_sizes, ctx.dim, streams)
        # Synchronize with the copy stream
        if streams is not None:
            for i, output in enumerate(outputs):
                with ddp_gpu.device(target_gpus[i]):
                    main_stream = ddp_gpu.current_stream()
                    main_stream.wait_stream(streams[i])
                    output.record_stream(main_stream)
        return outputs

    @staticmethod
    def backward(ctx, *grad_output):
        return None, None, None, Gather.apply(ctx.input_device, ctx.dim, *grad_output)


# background streams used for copying
_streams: Optional[List[Optional[torch.cuda.Stream]]] = None


def _get_stream(device: int):
    """Gets a background stream for copying between CPU and GPU"""
    global _streams
    ddp_gpu = _get_device_impl()[1]
    if device == -1:
        return None
    if _streams is None:
        _streams = [None] * ddp_gpu.device_count()
    if _streams[device] is None:
        _streams[device] = ddp_gpu.Stream(device)
    return _streams[device]
