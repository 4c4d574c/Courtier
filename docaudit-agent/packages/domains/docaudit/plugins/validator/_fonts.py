"""Shared font-family mappings for the validator package.

Centralised here so both the format checker and template CRUD module
reference a single authoritative set of font-name normalizations.
"""

# -- English / internal font name → canonical Chinese name --------------------
# python-docx font.name returns English names; audit rules use Chinese names.
FONT_FAMILY_EN_TO_CN: dict[str, str] = {
    "FangSong": "仿宋",
    "仿宋_GB2312": "仿宋",
    "SimFang": "仿宋",
    "SimSun": "宋体",
    "NSimSun": "新宋体",
    "SimHei": "黑体",
    "KaiTi": "楷体",
    "楷体_GB2312": "楷体",
    "SimKai": "楷体",
    "STSong": "宋体",
    "STFangsong": "仿宋",
    "STKaiti": "楷体",
    "STHeiti": "黑体",
    "STXihei": "黑体",
    "FZXiaoBiaoSong-B05": "小标宋",
    "FZXiaoBiaoSong-B05S": "小标宋",
    "方正小标宋简体": "小标宋",
    "方正小标宋_GBK": "小标宋",
}

# -- Chinese variant name → canonical name ------------------------------------
FONT_FAMILY_CN_ALIASES: dict[str, str] = {
    "小标宋体": "小标宋",
    "标宋": "小标宋",
}

# -- Canonical name → GB2312 / font-file name for template generation ---------
FONT_FAMILY_TO_FILE: dict[str, str] = {
    "仿宋": "仿宋_GB2312",
    "小标宋": "方正小标宋简体",
    "楷体": "楷体_GB2312",
    "黑体": "黑体",
    "宋体": "宋体",
    "方正小标宋简体": "方正小标宋简体",
    "仿宋_GB2312": "仿宋_GB2312",
    "楷体_GB2312": "楷体_GB2312",
    "Arial": "Arial",
    "Times New Roman": "Times New Roman",
}
