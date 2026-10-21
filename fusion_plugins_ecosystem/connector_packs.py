"""第一方连接器包目录（国内生态优先：飞书/钉钉/企微 + mail/Calendar/Notion）。

对应 issue #27「domestic-app connector packs」：
每个包都是 ConnectorPack 声明式定义（外部 MCP server 命令/端点 + 凭证注入 +
工具清单 + 风险等级），不含任何执行逻辑。命令为社区/官方 MCP server 的
默认值，可通过自定义 pack 覆盖。

凭证一律经 DeskRuntime 加密存储（Fernet，本地落盘），安装时不收集、
连接时才解密注入。
"""

from __future__ import annotations

from fusion_plugins_ecosystem.connector import (
    ConnectorPack,
    ConnectorToolSpec,
    RiskLevel,
)


def _tool(
    name: str,
    desc: str,
    props: dict,
    required: list[str],
    risk: RiskLevel = RiskLevel.LOW,
    read_only: bool = False,
) -> ConnectorToolSpec:
    return ConnectorToolSpec(
        name=name,
        description=desc,
        input_schema={"type": "object", "properties": props, "required": required},
        risk=risk,
        read_only=read_only,
    )


# ── 飞书（Lark）：官方 lark-mcp ──
FEISHU_PACK = ConnectorPack(
    id="feishu",
    name="飞书 Lark",
    version="1.0.0",
    description=(
        "飞书/Lark 连接器：读取消息与云文档、发送消息回写工作流"
        "（外部 MCP server: @larksuiteoapi/lark-mcp）"
    ),
    transport="stdio",
    command=("npx", "-y", "@larksuiteoapi/lark-mcp", "mcp"),
    env_credentials={
        "feishu_app_id": "LARK_APP_ID",
        "feishu_app_secret": "LARK_APP_SECRET",
    },
    homepage="https://github.com/larksuite/lark-openapi-mcp",
    tools=(
        _tool(
            "im.message.list",
            "读取会话消息列表（读消息入工作流）",
            {
                "chat_id": {"type": "string", "description": "会话 ID"},
                "page_size": {"type": "integer", "description": "每页条数"},
            },
            ["chat_id"],
            read_only=True,
        ),
        _tool(
            "im.message.send",
            "发送消息到会话（工作流结果回写）",
            {
                "chat_id": {"type": "string", "description": "会话 ID"},
                "content": {"type": "string", "description": "消息文本内容"},
            },
            ["chat_id", "content"],
            risk=RiskLevel.HIGH,
        ),
        _tool(
            "docx.document.read",
            "读取飞书云文档内容",
            {"document_id": {"type": "string", "description": "文档 ID"}},
            ["document_id"],
            read_only=True,
        ),
        _tool(
            "docx.document.create",
            "创建飞书云文档",
            {
                "title": {"type": "string", "description": "文档标题"},
                "content": {"type": "string", "description": "初始内容"},
            },
            ["title"],
            risk=RiskLevel.HIGH,
        ),
    ),
)

# ── 钉钉（DingTalk）──
DINGTALK_PACK = ConnectorPack(
    id="dingtalk",
    name="钉钉 DingTalk",
    version="1.0.0",
    description="钉钉连接器：群消息读取/发送、待办读写（社区 dingtalk MCP server）",
    transport="stdio",
    command=("npx", "-y", "dingtalk-mcp"),
    env_credentials={
        "dingtalk_app_key": "DINGTALK_APP_KEY",
        "dingtalk_app_secret": "DINGTALK_APP_SECRET",
    },
    homepage="https://open.dingtalk.com/",
    tools=(
        _tool(
            "conversation.message.list",
            "读取群会话消息",
            {"conversation_id": {"type": "string", "description": "群会话 ID"}},
            ["conversation_id"],
            read_only=True,
        ),
        _tool(
            "conversation.message.send",
            "发送消息到群会话",
            {
                "conversation_id": {"type": "string", "description": "群会话 ID"},
                "content": {"type": "string", "description": "消息内容"},
            },
            ["conversation_id", "content"],
            risk=RiskLevel.HIGH,
        ),
        _tool(
            "todo.list",
            "读取钉钉待办列表",
            {},
            [],
            read_only=True,
        ),
    ),
)

# ── 企业微信（WeCom）──
WECOM_PACK = ConnectorPack(
    id="wecom",
    name="企业微信 WeCom",
    version="1.0.0",
    description="企业微信连接器：应用消息收发、通讯录读取（社区 wecom MCP server）",
    transport="stdio",
    command=("npx", "-y", "wecom-mcp"),
    env_credentials={
        "wecom_corp_id": "WECOM_CORP_ID",
        "wecom_secret": "WECOM_SECRET",
    },
    homepage="https://work.weixin.qq.com/",
    tools=(
        _tool(
            "message.list",
            "读取应用消息",
            {"chat_id": {"type": "string", "description": "群聊 ID"}},
            ["chat_id"],
            read_only=True,
        ),
        _tool(
            "message.send",
            "发送应用消息",
            {
                "chat_id": {"type": "string", "description": "群聊 ID"},
                "content": {"type": "string", "description": "消息内容"},
            },
            ["chat_id", "content"],
            risk=RiskLevel.HIGH,
        ),
        _tool(
            "contact.list",
            "读取部门成员列表",
            {"department_id": {"type": "integer", "description": "部门 ID"}},
            [],
            read_only=True,
        ),
    ),
)

# ── 邮件（IMAP/SMTP）──
MAIL_PACK = ConnectorPack(
    id="mail",
    name="邮件 Mail",
    version="1.0.0",
    description="邮件连接器：IMAP 收件读取 + SMTP 发送（社区 imap/smtp MCP server）",
    transport="stdio",
    command=("npx", "-y", "imap-smtp-mcp"),
    env_credentials={
        "mail_username": "MAIL_USERNAME",
        "mail_password": "MAIL_PASSWORD",
    },
    homepage="https://modelcontextprotocol.io/",
    tools=(
        _tool(
            "mail.list",
            "读取收件箱邮件列表",
            {"folder": {"type": "string", "description": "邮箱文件夹（默认 INBOX）"}},
            [],
            read_only=True,
        ),
        _tool(
            "mail.read",
            "读取单封邮件全文",
            {"message_id": {"type": "string", "description": "邮件 ID"}},
            ["message_id"],
            read_only=True,
        ),
        _tool(
            "mail.send",
            "通过 SMTP 发送邮件",
            {
                "to": {"type": "string", "description": "收件人地址"},
                "subject": {"type": "string", "description": "邮件主题"},
                "body": {"type": "string", "description": "邮件正文"},
            },
            ["to", "subject", "body"],
            risk=RiskLevel.HIGH,
        ),
    ),
)

# ── 日历（CalDAV）──
CALENDAR_PACK = ConnectorPack(
    id="calendar",
    name="日历 Calendar",
    version="1.0.0",
    description="日历连接器：CalDAV 日程读取/创建（社区 caldav MCP server）",
    transport="stdio",
    command=("npx", "-y", "caldav-mcp"),
    env_credentials={
        "calendar_username": "CALDAV_USERNAME",
        "calendar_password": "CALDAV_PASSWORD",
    },
    homepage="https://modelcontextprotocol.io/",
    tools=(
        _tool(
            "calendar.list_events",
            "读取指定时间范围日程",
            {
                "start": {"type": "string", "description": "开始时间 (ISO 8601)"},
                "end": {"type": "string", "description": "结束时间 (ISO 8601)"},
            },
            ["start", "end"],
            read_only=True,
        ),
        _tool(
            "calendar.create_event",
            "创建日程",
            {
                "title": {"type": "string", "description": "日程标题"},
                "start": {"type": "string", "description": "开始时间 (ISO 8601)"},
                "end": {"type": "string", "description": "结束时间 (ISO 8601)"},
            },
            ["title", "start", "end"],
            risk=RiskLevel.MEDIUM,
        ),
    ),
)

# ── Notion（官方 @notionhq/notion-mcp-server）──
NOTION_PACK = ConnectorPack(
    id="notion",
    name="Notion",
    version="1.0.0",
    description="Notion 连接器：页面/数据库读取与写入（官方 notion-mcp-server）",
    transport="http",
    url="https://mcp.notion.com/mcp",
    env_credentials={"notion_token": "Authorization"},
    homepage="https://github.com/makenotion/notion-mcp-server",
    tools=(
        _tool(
            "search",
            "搜索 Notion 工作区页面/数据库",
            {"query": {"type": "string", "description": "搜索关键词"}},
            [],
            read_only=True,
        ),
        _tool(
            "fetch",
            "读取 Notion 页面内容",
            {"id": {"type": "string", "description": "页面/数据源 ID"}},
            ["id"],
            read_only=True,
        ),
        _tool(
            "create_pages",
            "创建 Notion 页面",
            {
                "parent": {"type": "object", "description": "父页面/数据库引用"},
                "properties": {"type": "object", "description": "页面属性"},
            },
            ["parent"],
            risk=RiskLevel.HIGH,
        ),
    ),
)

# 内置第一方目录（id → pack）
FIRST_PARTY_PACKS: dict[str, ConnectorPack] = {
    p.id: p
    for p in (
        FEISHU_PACK,
        DINGTALK_PACK,
        WECOM_PACK,
        MAIL_PACK,
        CALENDAR_PACK,
        NOTION_PACK,
    )
}


def get_builtin_pack(pack_id: str) -> ConnectorPack | None:
    """按 ID 取内置第一方连接器包。"""
    return FIRST_PARTY_PACKS.get(pack_id)


def list_builtin_packs() -> list[ConnectorPack]:
    """列出全部内置第一方连接器包。"""
    return list(FIRST_PARTY_PACKS.values())
