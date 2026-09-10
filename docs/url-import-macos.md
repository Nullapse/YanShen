# 粉笔套卷 URL 导入与 macOS 版本说明

本文说明本次新增的粉笔套卷 URL 自动导入功能、macOS 版本的正确下载方式，以及为什么不能只复制一个可执行文件运行。

## 原作者与内容声明

本项目原始代码和产品由 [Nullapse](https://github.com/Nullapse) 创建和维护。本次提交是在原项目基础上的增量修改，主要增加了粉笔套卷 URL 导入、登录态本机保存、参考答案候选导入、macOS 打包和相关界面调整。

原项目代码、题库、材料、参考答案和其他数据的著作权及来源权利，仍归原作者或各自原始出处所有。新增导入内容来自用户在粉笔等第三方平台拥有合法访问权限的页面，仅供个人学习、比较和研究使用。计划公开发布或再分发前，请先联系原作者并遵守仓库中的 `LICENSE`、第三方平台规则和内容来源要求。

## 新增功能

进入应用的“录入新套卷”页面后，可以直接粘贴粉笔套卷解析页 URL：

1. 在“来源页面 URL”输入完整的粉笔解析页或练习页地址。
2. 第一次导入或登录态失效时，展开“粉笔登录态”。
3. 将 Cookie-Editor 导出的粉笔 Cookie JSON 数组完整粘贴进去；也支持 `Cookie: sess=...; userid=...` 形式。
4. 如果粉笔提示 `DeviceSid` 无效，再填写浏览器登录态中的 DeviceSid。
5. 点击“直接导入并预览”，确认材料、题目、参考答案候选和踩分树后再保存套卷。

导入练习页时，如果该练习尚未交卷，研申会自动填写占位答案并提交粉笔，以获得该套卷每道题的 `scoreAnalysisVO` 踩分树。已经交卷的练习页和解析页会直接读取现有踩分树。批改时 AI 只对这套粉笔踩分树逐点判断命中或部分命中，不再自行划点或重算权重。

导入器会过滤非粉笔域名 Cookie，并且只有在同时解析到材料正文和题目时才允许导入。粉笔页面中的参考答案作为批改报告的展示标杆；AI 不再自行生成、改写或压缩完整答案。评分仍以题干、作答要求和原始材料为最高依据，白鹭与小马哥方法只用于拆点、归并和同义表达识别；粉笔答案中缺少材料依据的内容不得设为必得分点。

勾选“保存到本机，之后只输入 URL”后，登录态会保存到当前用户的本机数据目录中的 `fenbi-session.json`，文件权限会限制为当前用户可读写。Cookie 不会写入题库、导出数据、日志或 Git 仓库。共享电脑或不再使用时，可以点击“清除本机登录态”。

## macOS 版本的正确使用方式

### 必须下载完整压缩包

macOS 版本发布的是一个完整的 `研申.app` 应用包，而不是单独的二进制文件。请在 GitHub Release 中下载类似下面的完整压缩包：

```text
gongkao-shenlun-v1.4.3-macos-arm64.zip
```

下载后请按下面的顺序操作：

1. 用 macOS“归档实用工具”完整解压 ZIP。
2. 保持 `研申.app` 内部目录结构不变，直接双击 `研申.app`。
3. 首次被 Gatekeeper 拦截时，在 Finder 中右键应用并选择“打开”，再确认一次。
4. 不要从 `研申.app/Contents/MacOS/研申` 单独拷贝或运行文件，也不要只上传这个可执行文件。

完整 `.app` 内部包含以下运行资源：

- `Contents/Resources/data/gongkao_seed.sqlite3`：内置题库种子数据库；
- `Contents/Resources/static`：网页界面资源；
- `Contents/Resources/knowledge`：申论方法论和知识库；
- 应用运行所需的 Python、WebKit 和其他依赖库。

只下载或复制可执行文件时，`data/gongkao_seed.sqlite3` 等资源不会跟随，启动时就可能出现“数据库缺失”“资源文件不存在”等错误。因此发布和转移时必须以完整 `研申.app` 或完整 ZIP 为单位。

### Apple Silicon 与 Intel

当前 Release 的 macOS 构建标记为 `arm64`，适用于 Apple Silicon Mac（M1/M2/M3/M4）。Intel Mac 需要单独使用 x86_64 Python 环境重新构建，或等待仓库提供对应的 `macos-x86_64` 包；不要把架构不匹配的包强行复制到另一台机器上。

### 数据保存位置

应用首次启动时会把可写的个人数据复制到：

```text
~/.gongkao-shenlun/
```

其中包括个人数据库 `gongkao.sqlite3`、日志、WebView 配置和可选的 `fenbi-session.json`。这些是个人数据，不应随应用包上传到 GitHub。更新应用时替换完整的 `研申.app` 即可，通常不会影响这个目录中的个人记录；仍建议在大版本更新前备份该目录。

## 从源码构建 macOS 版本

如果 Release 中没有适合自己机器架构的包，可以在 macOS 上从源码构建：

```bash
git clone https://github.com/Nullapse/YanShen.git
cd YanShen
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-build.txt
./scripts/build_macos_app.sh
open "dist/研申.app"
```

构建脚本会先运行题库种子和知识库完整性检查，再把数据库和静态资源一起打进 `.app`。构建完成后请分发 `dist/研申.app` 的完整目录；如果要上传到 GitHub，建议压缩整个应用包：

```bash
ditto -c -k --sequesterRsrc --keepParent "dist/研申.app" "gongkao-shenlun-macos-arm64.zip"
```

## 使用前的安全提醒

Cookie 等登录凭据等同于临时登录权限。只从自己有权访问的粉笔账号导出，使用后可在应用中清除；如果 Cookie 曾经粘贴到公开 Issue、聊天记录或其他不受控位置，应立即退出粉笔账号或重新登录以使旧登录态失效。不要把 `fenbi-session.json`、API Key 或个人数据库提交到 GitHub。
