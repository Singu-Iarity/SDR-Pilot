"""SDR Pilot convert (installed into GNU Radio's block folder; used by the 'sp_convert' block).

Any stream type to any other - complex / float / int / short / byte - built from GNU Radio's native converter
blocks, so it runs at full speed. complex -> real types use `mode` (mag, mag_squared, real, imag, arg); real ->
complex puts the value in I (Q = 0). `scale` multiplies the value on the way (after reading, before writing)."""
from gnuradio import blocks, gr

SIZES = {"complex": gr.sizeof_gr_complex, "float": gr.sizeof_float, "int": gr.sizeof_int,
         "short": gr.sizeof_short, "byte": gr.sizeof_char}
TO_REAL = {"mag": blocks.complex_to_mag, "mag_squared": blocks.complex_to_mag_squared,
           "real": blocks.complex_to_real, "imag": blocks.complex_to_imag, "arg": blocks.complex_to_arg}


def _scaler(dtype: str, scale: float):
    if dtype == "complex":
        return blocks.multiply_const_cc(complex(scale))
    if dtype == "float":
        return blocks.multiply_const_ff(float(scale))
    if dtype == "int":
        return blocks.multiply_const_ii(int(round(scale)))
    if dtype == "short":
        return blocks.multiply_const_ss(int(round(scale)))
    return None


def chain(in_type: str, out_type: str, mode: str = "mag", scale: float = 1.0) -> list:
    """The native blocks (vlen 1) that turn in_type into out_type."""
    scale = float(scale)
    if in_type not in SIZES or out_type not in SIZES:
        raise ValueError(f"sp_convert: types are {list(SIZES)} (got {in_type!r} -> {out_type!r})")
    if in_type == out_type:
        s = _scaler(in_type, scale) if scale != 1.0 else None
        return [s] if s is not None else [blocks.copy(SIZES[in_type])]
    out: list = []
    # 1. to float
    if in_type == "complex":
        if mode not in TO_REAL:
            raise ValueError(f"sp_convert: mode must be one of {list(TO_REAL)}")
        out.append(TO_REAL[mode](1))
    elif in_type == "int":
        out.append(blocks.int_to_float(1, 1.0))
    elif in_type == "short":
        out.append(blocks.short_to_float(1, 1.0))
    elif in_type == "byte":
        out.append(blocks.uchar_to_float())
    # 2. scale, 3. from float
    if out_type == "complex":
        if scale != 1.0:
            out.append(blocks.multiply_const_ff(scale))
        out.append(blocks.float_to_complex(1))
    elif out_type == "float":
        if scale != 1.0:
            out.append(blocks.multiply_const_ff(scale))
    elif out_type == "int":
        out.append(blocks.float_to_int(1, scale))
    elif out_type == "short":
        out.append(blocks.float_to_short(1, scale))
    elif out_type == "byte":
        if scale != 1.0:
            out.append(blocks.multiply_const_ff(scale))
        out.append(blocks.float_to_uchar())
    return out


class convert(gr.hier_block2):
    def __init__(self, in_type="complex", out_type="float", mode="mag", scale=1.0, vlen=1):
        vlen = max(1, int(vlen))
        gr.hier_block2.__init__(self, "sp_convert", gr.io_signature(1, 1, SIZES[in_type] * vlen),
                                gr.io_signature(1, 1, SIZES[out_type] * vlen))
        parts = chain(in_type, out_type, mode, scale)
        if vlen > 1:  # the converters work on single samples: unpack the vectors and pack them again
            parts = [blocks.vector_to_stream(SIZES[in_type], vlen)] + parts + [
                blocks.stream_to_vector(SIZES[out_type], vlen)]
        self._parts = parts
        prev = self
        for b in parts:
            self.connect((prev, 0), (b, 0))
            prev = b
        self.connect((prev, 0), (self, 0))
