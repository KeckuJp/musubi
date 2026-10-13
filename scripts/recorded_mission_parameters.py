"""Nine fixed saved MAV_CMD declarations, never command creation or execution.

Parameter declarations agree at common.xml 3203f89c and 1c5b39f; callers retain
their format pin separately. Null is missing, not a protocol NaN instruction.
"""
from decimal import Decimal, InvalidOperation
import math

FIELDS = ['mission_parameter_basis', 'mission_parameter_definition_pin', 'mission_parameter_coverage']
for _i in range(1, 8):
    FIELDS += [f'declared_p{_i}_{s}' for s in ('role', 'unit', 'value', 'disposition', 'raw')]
E = ('unused', 'none')
POS = (('latitude', 'deg'), ('longitude', 'deg'), ('altitude', 'm'))
DEFINITIONS = {
    16: (('hold', 's'), ('accept_radius', 'm'), ('pass_radius', 'm'), ('yaw', 'deg')) + POS,
    20: (E,)*7,
    21: (('abort_altitude', 'm'), ('land_mode', 'PRECISION_LAND_MODE'), E, ('yaw', 'deg')) + POS,
    22: (('pitch', 'deg'), E, ('takeoff_flags', 'NAV_TAKEOFF_FLAGS'), ('yaw', 'deg')) + POS,
    178: (('speed_type', 'SPEED_TYPE'), ('speed', 'm/s'), ('throttle', '%')) + (('reserved', 'none'),)*4,
    179: (('use_current', 'MAV_BOOL'), ('surface_roll', 'deg'), ('surface_pitch', 'deg'), ('yaw', 'deg')) + POS,
    181: (('relay_instance', 'index'), ('relay_setting', 'hardware_setting')) + (E,)*5,
    183: (('servo_instance', 'index'), ('pulse_width', 'us')) + (E,)*5,
    189: (E,)*4 + POS,
}
BASES = {0: 'MSL_DECLARED', 3: 'HOME_RELATIVE_DECLARED', 10: 'TERRAIN_RELATIVE_DECLARED'}
ENUMS = {'speed_type': {0:'AIRSPEED',1:'GROUNDSPEED',2:'CLIMB_SPEED',3:'DESCENT_SPEED'},
         'land_mode': {0:'DISABLED',1:'OPPORTUNISTIC',2:'REQUIRED'},
         'relay_setting': {0:'OFF',1:'ON'}, 'use_current': {0:'SPECIFIED_LOCATION',1:'CURRENT_LOCATION'}}

def interpret(command, frame, params, *, definition_pin='3203f89c510337c0088244735c6a5056c52b5a28'):
    if definition_pin not in ('3203f89c510337c0088244735c6a5056c52b5a28', '1c5b39f6709166596341b855d92331c1c0885b5c'):
        raise ValueError('unqualified parameter definition')
    if type(command) is not int or type(frame) is not int or len(params) != 7:
        raise ValueError('integer command/frame and seven saved parameters required')
    output = dict.fromkeys(FIELDS, '')
    output['mission_parameter_basis'] = 'SAVED_DECLARATION_ONLY_NOT_SENT_OR_EXECUTED'
    output['mission_parameter_definition_pin'] = definition_pin
    output['mission_parameter_coverage'] = 'QUALIFIED_NINE_COMMAND_SET' if command in DEFINITIONS else 'UNINTERPRETED_COMMAND'
    values = []
    for raw in params:
        if raw is None:
            values.append(None); continue
        if type(raw) is bool:
            raise ValueError('boolean is not a saved numeric parameter')
        try:
            value = Decimal(str(raw))
        except InvalidOperation as error:
            raise ValueError('invalid saved parameter') from error
        if value.is_infinite() or (value.is_finite() and
                (not math.isfinite(float(value)) or (value != 0 and float(value) == 0))):
            raise ValueError('unrepresentable saved parameter')
        values.append(value)
    for index, (raw, value) in enumerate(zip(params, values), 1):
        prefix = f'declared_p{index}_'
        output[prefix+'raw'] = 'NULL' if raw is None else 'hex:'+str(raw).encode().hex()
        role, unit = DEFINITIONS.get(command, (('unknown', 'unknown'),)*7)[index-1]
        output[prefix+'role'], output[prefix+'unit'] = role, unit
        disposition = 'DECLARED'
        if command not in DEFINITIONS:
            disposition = 'UNINTERPRETED_COMMAND'
        elif role in ('unused', 'reserved'):
            disposition = 'UNUSED_RETAINED' if role == 'unused' else 'RESERVED_RETAINED'
        elif value is None:
            disposition = 'MISSING_NULL'
        elif value.is_nan():
            disposition = ('USE_DEFAULT_HEADING' if command == 179 and index == 4 else
                           'NOT_SET' if command == 179 and index in (2, 3) else
                           'USE_SYSTEM_YAW_MODE' if role == 'yaw' else 'UNQUALIFIED_NAN')
        else:
            if role in ('hold', 'accept_radius', 'relay_instance', 'servo_instance', 'pulse_width', 'relay_setting') and value < 0:
                raise ValueError('negative declared parameter outside fixed definition')
            if unit in ('index', 'us', 'hardware_setting', 'MAV_BOOL', 'SPEED_TYPE', 'PRECISION_LAND_MODE', 'NAV_TAKEOFF_FLAGS') and value != value.to_integral_value():
                raise ValueError('integer declaration required')
            if unit in ('index', 'us', 'hardware_setting', 'MAV_BOOL', 'SPEED_TYPE', 'PRECISION_LAND_MODE', 'NAV_TAKEOFF_FLAGS') and abs(value) > 2**53-1:
                raise ValueError('declared integer cannot be retained exactly in common number')
            if role == 'use_current' and value not in (0, 1):
                raise ValueError('invalid declared MAV_BOOL')
            if role in ('speed', 'throttle') and value < -2:
                raise ValueError('value below defined sentinel range')
            if role in ('speed', 'throttle') and value in (-1, -2):
                disposition = 'NO_CHANGE' if value == -1 else 'RETURN_TO_DEFAULT'
            elif role == 'abort_altitude' and value == 0:
                disposition = 'UNDEFINED_USE_SYSTEM_DEFAULT'
            elif command == 179 and index in (2, 3) and value == 0:
                disposition = 'NOT_SET'
            elif role in ('latitude', 'longitude', 'altitude'):
                if command == 179 and values[0] == 1:
                    disposition = 'IGNORED_USE_CURRENT_LOCATION'
                elif command == 179 and (values[0] is None or values[0].is_nan()):
                    disposition = 'WITHHELD_USE_CURRENT_UNKNOWN'
                elif command == 189 and values[4:7] == [Decimal(0)]*3:
                    disposition = 'USE_FIRST_LANDING_WAYPOINT'
                elif frame not in BASES:
                    disposition = 'WITHHELD_UNQUALIFIED_FRAME'
                else:
                    if role == 'latitude' and not -90 <= value <= 90 or role == 'longitude' and not -180 <= value <= 180:
                        raise ValueError('declared global coordinate out of range')
                    output[prefix+'unit'] = 'm_'+BASES[frame] if role == 'altitude' else 'deg_WGS84_DECLARED'
            if command == 179 and index in (2, 3, 4):
                bound = 90 if index == 3 else 180
                if not -bound <= value <= bound:
                    raise ValueError('declared surface angle out of range')
            if disposition == 'DECLARED':
                if command == 179 and index in (2, 3) and value == Decimal('0.01'):
                    output[prefix+'value'] = '0'
                    disposition = 'DECLARED_ZERO_ANGLE_SENTINEL'
                else:
                    output[prefix+'value'] = str(value)
                if role in ENUMS:
                    disposition = 'DECLARED_'+ENUMS[role].get(int(value), 'UNKNOWN_CODE_RETAINED')
                if role == 'takeoff_flags':
                    if value < 0:
                        raise ValueError('negative takeoff flags')
                    disposition = ('DECLARED_HORIZONTAL_POSITION_NOT_REQUIRED' if int(value)&1 else 'DECLARED_NO_KNOWN_FLAG')
                    if int(value)&~1:
                        disposition += '_UNKNOWN_BITS_RETAINED'
        output[prefix+'disposition'] = disposition
    return output
