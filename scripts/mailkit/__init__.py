"""PYLinTech/Postman-Skill 的内部实现包。

这一行 docstring 是文件存在的唯一理由：它让 mailkit 成为一个**常规包**。
没有 __init__.py 时它会退化成命名空间包（PEP 420），而命名空间包在
sys.path 上遇到同名常规包时会被后者覆盖——即使本目录排在前面。
这会让一个碰巧叫 mailkit 的第三方包让整个 skill 起不来。

保留此文件，不要往里加再导出：调用方一律使用子模块路径
（from mailkit import commands / from mailkit.mime import ...）。
"""
