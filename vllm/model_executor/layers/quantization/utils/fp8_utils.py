import torch
from typing import Optional, List, Tuple, Union

from vllm import _custom_ops as ops
from vllm.platforms import current_platform
from vllm.model_executor.utils import set_weight_attrs

def cutlass_fp8_supported() -> bool:
    capability = current_platform.get_device_capability()
    capability = capability[0] * 10 + capability[1]

    return ops.cutlass_scaled_mm_supports_fp8(capability)

def per_tensor_quantize(tensor: torch.Tensor,
                        inv_scale: Union[float, torch.Tensor]) -> torch.Tensor:
    finfo = torch.finfo(torch.float8_e4m3fn)
    qweight = (tensor / inv_scale).clamp(min=finfo.min, max=finfo.max)
    return qweight.to(torch.float8_e4m3fn)

def per_tensor_dequantize(
        tensor: torch.Tensor, inv_scale: Union[float,
                                               torch.Tensor]) -> torch.Tensor:
    fake_qweight = tensor.to(torch.float16)
    dq_weight = fake_qweight * inv_scale
    return dq_weight

def all_close_1d(x: torch.Tensor) -> bool:
    assert len(x.shape) == 1
    return all(torch.allclose(x[0], x[i]) for i in range(x.shape[0]))

def create_scale_param(output_partition_sizes: List[int],
                       **extra_weight_attrs) -> torch.nn.Parameter:
    scale = torch.nn.Parameter(torch.empty(len(output_partition_sizes),
                                           dtype=torch.float32),
                                           requires_grad=False)
    scale[:] = torch.finfo(torch.float8_e4m3fn).min
    set_weight_attrs(scale, {
        **extra_weight_attrs,
        "needs_scalar_to_array": True,
    })
    return scale

def requantize_with_max_scale(
    weight: torch.Tensor,
    weight_scale: torch.Tensor,
    logical_widths: List[int]
) -> Tuple[torch.Tensor, torch.Tensor]:
    # Max scale to be used for requanitzation.
    max_w_scale = weight_scale.max()

    # QKV / MLP is fused in the on disk checkpoint if any of the
    # weight scales are still set to the default since we initialize
    # N weight scales for N shards but we only load 1 weight scale
    # from disk in this case. Skip requantization in this case (since)
    # we already are quantized with the single scale.
    # * Sample Model: nm-testing/Phi-3-mini-128k-instruct-FP8
    unfused_module_in_checkpoint = (
        weight_scale[-1] > torch.finfo(torch.float8_e4m3fn).min)
    
    # If unfused checkpoint, need requanize with the single scale.
    if unfused_module_in_checkpoint:
        start = 0
        for idx, logical_width in enumerate(logical_widths):
            end = start + logical_width
            weight_dq = per_tensor_dequantize(weight[start:end, :], weight_scale[idx])
            weight[start:end, :] = per_tensor_quantize(weight_dq, max_w_scale)
            start = end

    return max_w_scale, weight

def fp8_apply(
    input: torch.Tensor,
    weight: torch.Tensor,
    weight_scale: torch.Tensor,
    input_scale: torch.Tensor,
    bias: Optional[torch.Tensor] = None,
    cutlass_fp8_supported: bool = True,
):
    # ops.scaled_fp8_quant supports both dynamic and static quant.
    #   If dynamic, layer.input_scale is None and x_scale computed from x.
    #   If static, layer.input_scale is scalar and x_scale is input_scale.

    if bias is None and cutlass_fp8_supported:
        qinput, x_scale = ops.scaled_fp8_quant(input, input_scale)

        # Fused GEMM_DQ
        output = ops.cutlass_scaled_mm(qinput,
                                       weight,
                                       out_dtype=input.dtype,
                                       scale_a=x_scale,
                                       scale_b=weight_scale)

    else:
        qinput, x_scale = ops.scaled_fp8_quant(input, input_scale,
                                               batch_dim_padding=17)

        # Fused GEMM_DQ -- note we padded the input above because
        # torch._scaled_mm is more performant for matrices with
        # batch dimension > 16. Note that this could change
        # in the future.
        output, _ = torch._scaled_mm(qinput,
                                     weight,
                                     out_dtype=input.dtype,
                                     scale_a=x_scale,
                                     scale_b=weight_scale,
                                     bias=bias)

    return torch.narrow(output, 0, 0, input.shape[0])
