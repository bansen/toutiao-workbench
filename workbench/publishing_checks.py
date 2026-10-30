"""Conservative DOM checks for the publishing verification gate.

These helpers do not publish. A native checkbox alone cannot establish that
an asynchronously rendered platform declaration is in the expected state.
"""
from .browser import NeedsAttention


def declaration_state(native_checked:bool,classes:str)->bool:
    rendered='byte-checkbox-checked' in (classes or '').split()
    if not isinstance(native_checked,bool) or native_checked!=rendered:
        raise NeedsAttention('声明控件的界面状态和输入状态不一致，须人工核实，未继续提交')
    return rendered


def check_publish_declarations(exclusive:dict,ai:dict):
    if declaration_state(exclusive.get('checked'),exclusive.get('classes','')):
        raise NeedsAttention('头条首发仍被选中，未继续提交')
    if not declaration_state(ai.get('checked'),ai.get('classes','')):
        raise NeedsAttention('引用AI尚未选中，未继续提交')
