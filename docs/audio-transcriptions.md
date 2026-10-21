# 语音转文字

`POST /v1/audio/transcriptions` 接收 `multipart/form-data`，通过 `Authorization: Bearer <API Key>` 或 `X-API-Key` 鉴权。音频和转写文本不写入用量日志、请求审计或最近请求记录，也不产生语音调用用量记录。Key 的状态、已有积分配额准入、请求频率和并发限制继续生效；此接口不假定上游免费或无限额。

| 字段 | 约定 |
| --- | --- |
| `file` | 必填，非空音频文件，网关上限 25 MiB；保留文件名、MIME 类型及原始字节传给上游 |
| `model` | 必填，`workbuddy-asr` |
| `response_format` | 可选，默认 `json`，也支持 `text` |

不支持其他字段（包括 `language`、`prompt`、`stream`、时间戳）；重复字段返回 400。无需手工设置 Content-Type，客户端必须生成 multipart boundary。

JSON 成功响应为 `{"text":"识别内容"}`；text 响应为 UTF-8 纯文本。未识别到语音时返回 200 和空文本，不自动换号重试。

认证失败返回 401，停用 Key 返回 403，Key 已有积分配额耗尽返回 402，参数或音频被拒绝返回 400，超过网关上传限制返回 413，限流返回 429，账号池不可用或上游故障返回 502/503。错误为 `{"error":{"message":"...","type":"...","code":"..."}}`（已有 Key 配额错误沿用网关格式）。上游识别能力、支持音频格式和用量限制以上游为准。

绑定 Key 仅使用其绑定账号；公共 Key 在所属健康账号池内选择，身份失败、限流和服务错误进行有限轮换。参数错误、空文件或成功的空转写不换号。不将上游错误原文写入账号健康记录，避免错误回显带出转写内容。

```sh
curl "$BASE_URL/v1/audio/transcriptions" \
  -H "Authorization: Bearer $API_KEY" \
  -F "file=@recording.webm" \
  -F "model=workbuddy-asr" \
  -F "response_format=json"
```

```python
from openai import OpenAI

client = OpenAI(api_key="<API Key>", base_url="http://127.0.0.1:8790/v1")
with open("recording.webm", "rb") as audio:
    result = client.audio.transcriptions.create(model="workbuddy-asr", file=audio)
print(result.text)
```
