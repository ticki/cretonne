"""
Cretonne shared settings.

This module defines settings are are relevant for all code generators.
"""
from __future__ import absolute_import
from . import SettingGroup, BoolSetting, EnumSetting

group = SettingGroup('shared')

opt_level = EnumSetting(
        """
        Optimization level:

        - default: Very profitable optimizations enabled, none slow.
        - best: Enable all optimizations
        - fastest: Optimize for compile time by disabling most optimizations.
        """,
        'default', 'best', 'fastest')

is_64bit = BoolSetting("Enable 64-bit code generation")

enable_float = BoolSetting(
        """Enable the use of floating-point instructions""",
        default=True)

enable_simd = BoolSetting(
        """Enable the use of SIMD instructions.""",
        default=True)

enable_atomics = BoolSetting(
        """Enable the use of atomic instructions""",
        default=True)

group.close(globals())
