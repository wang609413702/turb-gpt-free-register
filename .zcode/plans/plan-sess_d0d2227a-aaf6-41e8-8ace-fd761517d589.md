# 新增 MoMo 检测专用代理池

## 核心策略
配置 UI 是 schema 驱动的(`EDITABLE_FIELDS` 是单一数据源),所以「参考现有代理池样式」= 加一个 `list_str_multiline` 字段,**自动复用现有 textarea 渲染**(`renderConfigPlainFieldV2`)、保存(`saveConfigUpdates`)、读取(`loadConfig`)逻辑,无需新写 UI 代码。代理格式 `host:port:user:pass` 自动补 `socks5h://` 前缀的归一化逻辑放在**消费侧**(MoMo service 取用时),保持配置 schema 通用、可测试。

---

## 一、`config/proxy.py`(新增后端 key + pick 函数)
- 新增 `MOMO_PROXY_POOL = []`(默认空,与 PROXY_POOL 区分;空表示直连)。
- 新增 `pick_momo_proxy() -> str`:从池随机抽一个;**空返回 ""**(直连)。
- 新增 `normalize_momo_proxy(raw) -> str`:归一化单行代理 ——
  - 已有 `://` → 原样(但 `socks5://` → `socks5h://`,与 BrowserSession 一致);
  - 无 scheme 且形如 `host:port:user:pass`(4 段)或 `host:port`(2 段,无认证) → 补 `socks5h://` 前缀;
  - 空/非法 → 返回 ""(跳过)。
- `apply_env_overrides` 注册表加 `'MOMO_PROXY_POOL': 'list_str_multiline'`。

## 二、`webui/config_editor.py`(注册可编辑字段)
- 在 PROXY_POOL 字段后(第 430 行附近)新增:
  ```python
  {
      "key": "MOMO_PROXY_POOL", "file": "proxy.py", "type": "list_str_multiline", "group": "代理池",
      "label": "MoMo检测代理池(每行一个)", "help": "每行一个代理；支持 socks5/socks5h 完整 URL，或 host:port:user:pass 自动补 socks5h://。MoMo 检测单独使用此池；留空则直连",
  },
  ```
- `EXPLICIT_EMPTY_LIST_KEYS` 加 `"MOMO_PROXY_POOL"`(保证能清空,行为同 PROXY_POOL)。
- group 为「代理池」,自动出现在现有分组里,无需改 group-intro/nav-icon。

## 三、`core/momo_check_service.py`(检测时使用专用代理池)
- 改 `_run_account_momo_check`:`proxy is None`(前端未显式传)时,调 `pick_momo_proxy()` 取一个代理并归一化,作为非 None 的 `proxy` 传给 `check_account_momo`。这样 `resolve_plan_check_route` 会把它当显式覆盖(`proxy_mode="request"`),**绕开 PLAN_CHECK 配置,只用 MoMo 池**。池空 → 传 `""` → 直连。

## 四、不改动
- 不动前端 textarea 渲染/保存/读取(schema 驱动自动生效)。
- 不动 `resolve_plan_check_route`(复用,通过传非 None proxy 触发显式覆盖分支)。
- 不动 momo-check 路由(继续透传 `proxy`,默认 None)。

## 验证
`python3 -c "import config.proxy as p; ..."` 测 `normalize_momo_proxy` 各格式;`py_compile` 全部改动文件;`create_app()` 确认 `/api/config` 返回含 `MOMO_PROXY_POOL` 字段。