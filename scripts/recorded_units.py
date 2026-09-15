"""Finite source-qualified engineering units shared by passive export converters."""
import math

UNITS = {"V": ("voltage_v", 1, 0), "A": ("current_a", 1, 0),
         "K": ("temperature_k", 1, 0), "degK": ("temperature_k", 1, 0),
         "degC": ("temperature_k", 1, 273.15), "m": ("distance_m", 1, 0),
         "rpm": ("angular_speed_rad_s", math.pi / 30, 0),
         "mV": ("voltage_v", .001, 0), "mA": ("current_a", .001, 0),
         "mm": ("distance_m", .001, 0), "cm": ("distance_m", .01, 0),
         "m/s": ("speed_m_s", 1, 0), "km/h": ("speed_m_s", 1 / 3.6, 0),
         "rad": ("angle_rad", 1, 0), "deg": ("angle_rad", math.pi / 180, 0),
         "rad/s": ("angular_speed_rad_s", 1, 0), "deg/s": ("angular_speed_rad_s", math.pi / 180, 0),
         "Pa": ("pressure_pa", 1, 0), "hPa": ("pressure_pa", 100, 0),
         "kPa": ("pressure_pa", 1000, 0), "bar": ("pressure_pa", 100000, 0),
         "%": ("reported_ratio", .01, 0), "J": ("energy_j", 1, 0), "Wh": ("energy_j", 3600, 0)}


def normalize(value, unit):
    field, scale, offset = UNITS[unit]
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError("finite engineering quantity required")
    identity = scale == 1 and offset == 0
    if not identity and type(value) is int and abs(value) > 2**53:
        raise ValueError("scaled integer exceeds exact conversion input bound")
    result = value if identity else value * scale + offset
    if (not math.isfinite(result) or (field == "temperature_k" and result < 0)
            or (type(result) is int and not -(2**63) <= result < 2**63)):
        raise ValueError("normalized engineering quantity exceeds common range")
    return field, result
