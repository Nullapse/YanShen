# DeepSeek API 配置教程

本工具的智能批改使用 OpenAI-compatible Chat Completions 接口。DeepSeek 官方 API 文档入口：

- DeepSeek API Docs: <https://api-docs.deepseek.com/>
- DeepSeek Platform: <https://platform.deepseek.com/>

## 1. 注册并进入控制台

1. 打开 <https://platform.deepseek.com/>。
2. 登录或注册 DeepSeek 账号。
3. 进入控制台后，确认账号余额或充值状态可用。

## 2. 创建 API Key

1. 在 DeepSeek Platform 中打开 **API Keys** 页面。
2. 点击创建新的 API Key。
3. 复制生成的 Key，并妥善保存。页面通常只会完整展示一次。

不要把 API Key 填进 README、截图、导出文件或公开仓库。

## 3. 在“研申”中配置

打开应用中的 **设置** 页面，使用以下配置：

- 运行模式：`api`
- API 批改能力：`智能批改`
- 服务商：`DeepSeek`
- API Base URL：`https://api.deepseek.com`
- 模型：`deepseek-v4-pro`
- API Key：粘贴你刚创建的 Key

也可以不在界面里明文保存 Key，而是在系统环境变量中设置：

```powershell
setx DEEPSEEK_API_KEY "你的 DeepSeek API Key"
```

重新打开应用后，设置页保留 `API Key 环境变量名` 为 `DEEPSEEK_API_KEY` 即可。

## 4. 测试连接

1. 在设置页点击 **测试连接**。
2. 显示连接成功后，回到批改工作台。
3. 在作答详情页点击 **智能批改** 下的 **开始智能批改**。

## 5. 常见问题

- **提示未找到 API Key**：检查界面 API Key 是否填写，或 `DEEPSEEK_API_KEY` 是否在重新打开应用后生效。
- **请求失败或余额不足**：进入 DeepSeek Platform 检查余额、账单和 Key 状态。
- **返回格式不兼容**：确认 API Base URL 为 `https://api.deepseek.com`，不要填网页聊天地址。
- **不想上传作答**：使用 Codex 手动模式。本工具只有你主动点击智能批改时，才会发送当前题必要数据和本地检索出的少量证据。
- **智能批改失败**：可重试，或在设置中切换为“基础批改”使用原有链路。
