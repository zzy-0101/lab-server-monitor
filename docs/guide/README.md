# 服务器监控手册源码

主文档为项目中的 `docs/guide/main.tex`，插图在同级 `images/`。TeX 源码使用 XeLaTeX、ctex 和 Fandol 字体，插图来自真实 GUI 的示例模式，使用虚构账号和数值，不包含任何密码或真实任务。

完整重建需要 TeX Live / MacTeX（含 ctex、Fandol、adobemapping、graphicx、enumitem、caption、needspace 等常用包），以及 Python 3.9+。
在项目根目录打开终端，执行：

```bash
python3 -m pip install -r tools/requirements-docs.txt
python3 tools/build_guide.py
```

Windows 将 `python3` 换成可用的 `python` 或 `py -3`，并确认 `xelatex` 和 `kpsewhich` 在 PATH 中。
输出为 `docs/guide/服务器监控入门指南.pdf`；中间文件在同目录 `.build/`，可随时清理。

构建脚本会运行两次 XeLaTeX，再嵌入标准的 Adobe GB1 中文字符映射，使没有额外中文映射包的阅读器也能显示、搜索和复制文字。映射文件取自 TeX Live，保留原有版权许可声明。
直接用编辑器编译 `main.tex` 也可生成排版；如果某个阅读器显示空白中文或复制乱码，使用上面的完整构建流程。

正文参考原《服务器使用指南》（刘雷轶男、智朝晖，2024-12-30）及监控程序当前实现。原 PDF 保存在完整项目的 `docs/references/`，不包含在源码包中。
