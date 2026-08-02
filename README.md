# 研申

![研申主页截图](assets/app-home.png)

面向 Windows 的本地申论练习应用，内置国考、省考和选调申论试卷。支持试卷检索、材料阅读、作答记录、批注、收藏、训练统计、智能批改和 AI 训练复盘。

## 下载与使用

1. 在仓库右侧打开 **Releases**，下载最新的 `gongkao-shenlun-v*-windows-x64.zip`。
2. 完整解压 ZIP，双击 `研申.exe`。
3. 应用默认进入主页；关闭窗口后本地服务会自动退出。

应用无需安装 Python 或 Edge 浏览器，但 Windows 需要 [Microsoft WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)。个人数据保存在 `%LOCALAPPDATA%\GongkaoShenlun`，更新版本时直接替换程序目录即可。

## 主要功能

- 按试卷、地区、年份、题型、状态、关键词和答案来源筛选
- 阅读整卷材料并定位题目引用范围
- 作答草稿自动保存，多次作答可继续修改
- 材料高亮、文本批注、收藏和个人笔记
- 参考答案对比、训练统计和数据导入导出
- 基于本题参考答案共识、材料依据和个人历史的智能批改
- 逐点纠错、评分重算与 AI 训练复盘

## 智能批改

配置方法见 [DeepSeek API 配置教程](docs/deepseek-api-setup.md)。检索、参考答案聚类和证据筛选在本地完成；只有主动使用智能批改或 AI 功能时，当前题必要数据和少量命中证据才会发送给所配置的模型服务。API Key 不应提交到仓库。

## 源码运行与本地构建

```powershell
pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:5000`。可用 `GONGKAO_DATA_DIR` 指定个人数据目录，或用 `GONGKAO_DB_PATH` 指定测试数据库。

构建 Windows 可执行程序：

```powershell
pip install pyinstaller jinja2
pyinstaller --clean --noconfirm "研申.spec"
```

## 数据与许可

程序代码使用 [MIT License](LICENSE)。题目、材料和参考答案的相关权利归原作者或原出处所有，仅供个人学习、比较和研究；内容可能存在错字、缺漏或来源差异，请结合原始来源校验。公开数据库不包含私人作答、收藏、批改记录、API Key、课程原始讲义或本机路径。如需补充署名、修正来源或移除内容，请通过 GitHub Issue 联系维护者。
