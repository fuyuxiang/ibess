"""当前发布的规则参数契约，同时用于服务端校验及前端表单。"""
import math
from fastapi import HTTPException

from app.core.rule_parameters import PARAMETERS


def validate_parameters(code, parameters):
    if not isinstance(parameters, dict):
        raise HTTPException(422, '规则参数必须为JSON对象')
    if code not in PARAMETERS:
        return
    fields = {field['key']: field for field in PARAMETERS[code]}
    if set(parameters) - set(fields):
        raise HTTPException(422, '包含该规则不支持的参数，请按参数表单核对')
    for key, value in parameters.items():
        field = fields[key]
        if (isinstance(value, bool) or not isinstance(value, (int,float)) or not math.isfinite(value)
                or (field['type'] == 'integer' and not isinstance(value,int)) or not field['min'] <= value <= field['max']):
            raise HTTPException(422, f"{field['label']}须为{field['min']}至{field['max']}之间的{'整数' if field['type']=='integer' else '数值'}")
