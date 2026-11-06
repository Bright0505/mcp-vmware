# mcp-vmware

一個**唯讀**的 MCP server,讓 Claude 能查詢 VMware vCenter 環境。

- 只讀不控 — 不提供任何開關機、修改、刪除等操作。
- 透過 vSphere Automation **REST API**(`/api/vcenter/*`)呼叫 vCenter。
- 僅發出 `GET` 請求查詢資訊(唯一例外是 session 登入/登出)。
- 遵循 **MCP 2026-07-28** 規範(MCP Python SDK 2.x)。

## MCP 2.x 規範對應

| 規範要點 | 本專案實作 |
|---|---|
| Stateless protocol core(2026-07-28) | 使用 SDK 2.x 的 `MCPServer`,協定版本 `2026-07-28` |
| Tool annotations | 所有 tool 標註 `readOnlyHint=true`、`destructiveHint=false`、`idempotentHint=true` |
| Structured output | 每個 tool 都有 `outputSchema`,回傳 `structuredContent`(見 `models.py`) |
| Tool title / server title | server 與 tool 皆提供人類可讀的 `title` |
| Cacheable list results | `tools/list`、`server/discover` 帶 cache hint(TTL 1 小時、`public`) |
| Lifespan 資源管理 | vCenter session 由 server lifespan 建立,結束時自動登出 |

## 需求

- Python 3.10+
- vCenter Server 7.0 / 8.0(使用 `/api/vcenter/*` REST 路徑)
- 一組具備唯讀(Read-only)權限的 vCenter 帳號即可

## 安裝

```bash
pip install -e .
```

相依套件:`mcp>=2.2.0`、`httpx2`(MCP SDK 2.x 隨附的 HTTP client)、`pydantic`。

## 設定

透過環境變數提供連線資訊(可參考 `.env.example`):

| 變數 | 必填 | 說明 |
|---|---|---|
| `VCENTER_HOST` | 是 | vCenter FQDN 或 IP,例 `vcenter.example.com` |
| `VCENTER_USER` | 是 | 帳號,例 `administrator@vsphere.local` |
| `VCENTER_PASSWORD` | 是 | 密碼 |
| `VCENTER_INSECURE` | 否 | 設為 `true` 時關閉 TLS 憑證驗證(內部自簽憑證環境用),預設 `false` |

## 啟動

```bash
python -m mcp_vmware
```

以 stdio transport 執行,通常由 MCP 用戶端(Claude Code / Claude Desktop)自動啟動。

## 接入 Claude Code

```bash
claude mcp add vmware \
  -e VCENTER_HOST=vcenter.example.com \
  -e VCENTER_USER=administrator@vsphere.local \
  -e VCENTER_PASSWORD=yourpassword \
  -- python -m mcp_vmware
```

## 接入 Claude Desktop

Claude Desktop 由 app 直接啟動 server 行程,**不會**繼承終端機的 `PATH` 與虛擬環境 —
`command` 請填 Python 直譯器的**絕對路徑**(有用虛擬環境就填該環境下的 python),
否則會出現找不到指令或 `No module named mcp_vmware`。

取得絕對路徑:

```bash
# 先啟用安裝本專案的環境,再執行
python -c "import sys; print(sys.executable)"
```

### 設定步驟

1. 開啟 Claude 選單(macOS 在系統最上方的選單列)→ **Settings…** → 左側 **Developer** →
   按 **Edit Config**,會開啟設定檔:
   - macOS:`~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows:`%APPDATA%\Claude\claude_desktop_config.json`

2. 在 `mcpServers` 底下加入 `vmware`(把路徑與帳密換成自己的):

   macOS / Linux:

   ```json
   {
     "mcpServers": {
       "vmware": {
         "command": "/Users/yourname/git/mcp-vmware/.venv/bin/python",
         "args": ["-m", "mcp_vmware"],
         "env": {
           "VCENTER_HOST": "vcenter.example.com",
           "VCENTER_USER": "readonly@vsphere.local",
           "VCENTER_PASSWORD": "yourpassword",
           "VCENTER_INSECURE": "false"
         }
       }
     }
   }
   ```

   Windows(路徑的反斜線要寫成 `\\`):

   ```json
   {
     "mcpServers": {
       "vmware": {
         "command": "C:\\Users\\yourname\\git\\mcp-vmware\\.venv\\Scripts\\python.exe",
         "args": ["-m", "mcp_vmware"],
         "env": {
           "VCENTER_HOST": "vcenter.example.com",
           "VCENTER_USER": "readonly@vsphere.local",
           "VCENTER_PASSWORD": "yourpassword",
           "VCENTER_INSECURE": "false"
         }
       }
     }
   }
   ```

3. **完全結束**(Quit,不是關視窗)Claude Desktop 後重新開啟。

4. 在對話輸入框左下角點 **Add files, connectors, and more** → **Connectors** →
   **Manage connectors**,選 `vmware` 就會看到本 server 的 9 個唯讀工具。

### 試著問問看

- 「現在有幾台 VM 是開機狀態?」
- 「列出所有 ESXi 主機和它們的連線狀態」
- 「哪個 datastore 的剩餘空間最少?」

### 疑難排解

看不到 `vmware` 或工具呼叫失敗時,先看 log:

```bash
# macOS
tail -n 50 -f ~/Library/Logs/Claude/mcp*.log
```

```powershell
# Windows
type "%APPDATA%\Claude\logs\mcp-server-vmware.log"
```

再用同一組路徑與環境變數手動啟動一次,確認 server 本身沒問題:

```bash
VCENTER_HOST=vcenter.example.com \
VCENTER_USER=readonly@vsphere.local \
VCENTER_PASSWORD=yourpassword \
/Users/yourname/git/mcp-vmware/.venv/bin/python -m mcp_vmware
```

正常情況下不會有任何輸出(server 在等 stdin),按 `Ctrl-C` 結束即可。

常見原因:

| 症狀 | 原因 |
|---|---|
| 完全看不到這個 connector | `claude_desktop_config.json` JSON 格式錯誤(多一個逗號就整份不載入) |
| log 出現 `No such file` / `No module named mcp_vmware` | `command` 用了相對路徑,或該直譯器沒裝本專案 |
| log 出現「缺少必要環境變數: …」 | `env` 少填 `VCENTER_HOST` / `VCENTER_USER` / `VCENTER_PASSWORD` |
| TLS 憑證錯誤 | 自簽憑證環境請設 `"VCENTER_INSECURE": "true"` |

## 用 MCP Inspector 測試

```bash
npx @modelcontextprotocol/inspector python -m mcp_vmware
```

## 提供的 Tool(全部唯讀)

| Tool | 說明 | 回傳型別 |
|---|---|---|
| `list_vms` | 列出虛擬機摘要(可依電源狀態、名稱過濾) | `VMSummary[]` |
| `get_vm` | 取得單一 VM 詳情(硬體、電源、客體 OS) | `VMDetail` |
| `list_hosts` | 列出 ESXi 主機(可依連線狀態過濾) | `HostSummary[]` |
| `list_clusters` | 列出叢集 | `ClusterSummary[]` |
| `list_datastores` | 列出 Datastore 容量資訊 | `DatastoreSummary[]` |
| `list_networks` | 列出網路 / Port Group | `NetworkSummary[]` |
| `list_datacenters` | 列出資料中心 | `DatacenterSummary[]` |
| `list_resource_pools` | 列出資源池 | `ResourcePoolSummary[]` |
| `list_folders` | 列出資料夾 | `FolderSummary[]` |

回傳模型的欄位名稱與 vSphere REST API 一致,且允許額外欄位 —
不同 vCenter 版本多回的欄位會原樣保留,不會被丟棄或造成驗證失敗。

## 安全說明

- 本 server 不會對 vCenter 做任何異動;`vcenter.py` 中除 session 登入(`POST /api/session`)
  與登出(`DELETE /api/session`)外,僅有 `GET` 請求。
- 所有 tool 皆標註 `readOnlyHint`,用戶端可據此判斷本 server 不會改動環境。
- 建議搭配僅具唯讀權限的 vCenter 服務帳號使用,以從權限層面再加一道保險。
- Claude Desktop / Claude Code 的設定檔會以明文存放 `VCENTER_PASSWORD`,請勿把設定檔提交進版控;
  macOS 可再收緊權限:`chmod 600 ~/Library/Application\ Support/Claude/claude_desktop_config.json`。

## HTTP 模式(Open WebUI 原生 MCP 連線)

除了 stdio,也可用 Streamable HTTP 執行,端點 `/mcp/`:

```bash
PYTHONPATH=src HTTP_PORT=8000 MCP_AUTH_TOKEN=<token> python -m http_server
```

- 驗證:請求需帶 `Authorization: Bearer <MCP_AUTH_TOKEN>`(可逗號分隔多組以便輪替)。
  未設 `MCP_AUTH_TOKEN` 時不驗證並在 log 警告 —— 任何連得到的人都能讀整個 vCenter inventory,只限本機除錯。
  另支援選用的 OAuth access token(`MCP_OAUTH_ISSUER`／`MCP_OAUTH_AUDIENCE`／`MCP_OAUTH_JWKS_URL`,三者皆設定才啟用),
  詳見 `src/mcp_vmware/auth.py`。
- `/health` 免驗證,供容器健康檢查使用。
- 缺 `VCENTER_HOST`／`VCENTER_USER`／`VCENTER_PASSWORD` 時程序直接結束(健康檢查會失敗)。
- 無狀態(stateless)、JSON 回應;全程序共用一個 vCenter session。
- 工具名不帶前綴;Open WebUI 的函式名是「連線識別碼＋底線＋工具名」,例 `vmware_list_vms`。
- 所有 `list_*` 工具都回傳物件而非裸陣列:附 `total` 與依狀態／類型分組的計數(`list_datastores` 另附 GiB 換算、
  `used_pct`,並已依使用率由高到低排序)。數量一律由工具算好 —— LLM 自行計算長清單
  容易數錯。`list_vms` 另有 `name_contains`(不分大小寫的部分名稱比對)。

### 容器與部署

```bash
docker build --target production -t mcp-vmware .

docker run -d --name mcp-vmware -p 8000:8000 \
  -e VCENTER_HOST=vcenter.example.com \
  -e VCENTER_USER=readonly@vsphere.local \
  -e VCENTER_PASSWORD=yourpassword \
  -e MCP_AUTH_TOKEN=<token> \
  mcp-vmware

curl http://localhost:8000/health
```

- 容器內預設執行 `python -m http_server`,監聽 `HTTP_PORT`(預設 `8000`)。
- 建議以 `--env-file .env` 或部署平台的 secret 機制注入 `VCENTER_*` 與 `MCP_AUTH_TOKEN`,勿寫進 image。
- 依賴版本以 `constraints.txt` 鎖定;有意升級時在建好的 image 內 `pip freeze` 重新產生。

## 授權

[MIT](LICENSE)
