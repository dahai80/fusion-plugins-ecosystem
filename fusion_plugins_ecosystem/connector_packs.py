# First-party connector pack catalog (domestic-ecosystem first:
# Feishu/DingTalk/WeCom + Mail/Calendar/Notion).
#
# Per issue #27 "domestic-app connector packs": each pack is a declarative
# ConnectorPack definition (external MCP server command/endpoint +
# credential injection + tool manifest + risk level) with no execution
# logic. Commands are defaults for community/official MCP servers and can
# be overridden via custom packs.
#
# Credentials are always stored encrypted via DeskRuntime (Fernet, local
# on-disk); nothing is collected at install time and they are only
# decrypted and injected at connect time.

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


# ── Feishu (Lark): official lark-mcp ──
FEISHU_PACK = ConnectorPack(
    id="feishu",
    # i18n: user-facing
    name="飞书 Lark",
    version="1.0.0",
    # i18n: user-facing
    description=("飞书/Lark 连接器：读取消息与云文档、发送消息回写工作流（外部 MCP server: @larksuiteoapi/lark-mcp）"),
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
            # i18n: user-facing
            "读取会话消息列表（读消息入工作流）",
            {
                # i18n: user-facing
                "chat_id": {"type": "string", "description": "会话 ID"},
                # i18n: user-facing
                "page_size": {"type": "integer", "description": "每页条数"},
            },
            ["chat_id"],
            read_only=True,
        ),
        _tool(
            "im.message.send",
            # i18n: user-facing
            "发送消息到会话（工作流结果回写）",
            {
                # i18n: user-facing
                "chat_id": {"type": "string", "description": "会话 ID"},
                # i18n: user-facing
                "content": {"type": "string", "description": "消息文本内容"},
            },
            ["chat_id", "content"],
            risk=RiskLevel.HIGH,
        ),
        _tool(
            "docx.document.read",
            # i18n: user-facing
            "读取飞书云文档内容",
            {
                # i18n: user-facing
                "document_id": {"type": "string", "description": "文档 ID"}
            },
            ["document_id"],
            read_only=True,
        ),
        _tool(
            "docx.document.create",
            # i18n: user-facing
            "创建飞书云文档",
            {
                # i18n: user-facing
                "title": {"type": "string", "description": "文档标题"},
                # i18n: user-facing
                "content": {"type": "string", "description": "初始内容"},
            },
            ["title"],
            risk=RiskLevel.HIGH,
        ),
    ),
)

# ── DingTalk ──
DINGTALK_PACK = ConnectorPack(
    id="dingtalk",
    # i18n: user-facing
    name="钉钉 DingTalk",
    version="1.0.0",
    # i18n: user-facing
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
            # i18n: user-facing
            "读取群会话消息",
            {
                # i18n: user-facing
                "conversation_id": {"type": "string", "description": "群会话 ID"}
            },
            ["conversation_id"],
            read_only=True,
        ),
        _tool(
            "conversation.message.send",
            # i18n: user-facing
            "发送消息到群会话",
            {
                # i18n: user-facing
                "conversation_id": {"type": "string", "description": "群会话 ID"},
                # i18n: user-facing
                "content": {"type": "string", "description": "消息内容"},
            },
            ["conversation_id", "content"],
            risk=RiskLevel.HIGH,
        ),
        _tool(
            "todo.list",
            # i18n: user-facing
            "读取钉钉待办列表",
            {},
            [],
            read_only=True,
        ),
    ),
)

# ── WeCom ──
WECOM_PACK = ConnectorPack(
    id="wecom",
    # i18n: user-facing
    name="企业微信 WeCom",
    version="1.0.0",
    # i18n: user-facing
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
            # i18n: user-facing
            "读取应用消息",
            {
                # i18n: user-facing
                "chat_id": {"type": "string", "description": "群聊 ID"}
            },
            ["chat_id"],
            read_only=True,
        ),
        _tool(
            "message.send",
            # i18n: user-facing
            "发送应用消息",
            {
                # i18n: user-facing
                "chat_id": {"type": "string", "description": "群聊 ID"},
                # i18n: user-facing
                "content": {"type": "string", "description": "消息内容"},
            },
            ["chat_id", "content"],
            risk=RiskLevel.HIGH,
        ),
        _tool(
            "contact.list",
            # i18n: user-facing
            "读取部门成员列表",
            {
                # i18n: user-facing
                "department_id": {"type": "integer", "description": "部门 ID"}
            },
            [],
            read_only=True,
        ),
    ),
)

# ── Mail (IMAP/SMTP) ──
MAIL_PACK = ConnectorPack(
    id="mail",
    # i18n: user-facing
    name="邮件 Mail",
    version="1.0.0",
    # i18n: user-facing
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
            # i18n: user-facing
            "读取收件箱邮件列表",
            {
                # i18n: user-facing
                "folder": {"type": "string", "description": "邮箱文件夹（默认 INBOX）"}
            },
            [],
            read_only=True,
        ),
        _tool(
            "mail.read",
            # i18n: user-facing
            "读取单封邮件全文",
            {
                # i18n: user-facing
                "message_id": {"type": "string", "description": "邮件 ID"}
            },
            ["message_id"],
            read_only=True,
        ),
        _tool(
            "mail.send",
            # i18n: user-facing
            "通过 SMTP 发送邮件",
            {
                # i18n: user-facing
                "to": {"type": "string", "description": "收件人地址"},
                # i18n: user-facing
                "subject": {"type": "string", "description": "邮件主题"},
                # i18n: user-facing
                "body": {"type": "string", "description": "邮件正文"},
            },
            ["to", "subject", "body"],
            risk=RiskLevel.HIGH,
        ),
    ),
)

# ── Calendar (CalDAV) ──
CALENDAR_PACK = ConnectorPack(
    id="calendar",
    # i18n: user-facing
    name="日历 Calendar",
    version="1.0.0",
    # i18n: user-facing
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
            # i18n: user-facing
            "读取指定时间范围日程",
            {
                # i18n: user-facing
                "start": {"type": "string", "description": "开始时间 (ISO 8601)"},
                # i18n: user-facing
                "end": {"type": "string", "description": "结束时间 (ISO 8601)"},
            },
            ["start", "end"],
            read_only=True,
        ),
        _tool(
            "calendar.create_event",
            # i18n: user-facing
            "创建日程",
            {
                # i18n: user-facing
                "title": {"type": "string", "description": "日程标题"},
                # i18n: user-facing
                "start": {"type": "string", "description": "开始时间 (ISO 8601)"},
                # i18n: user-facing
                "end": {"type": "string", "description": "结束时间 (ISO 8601)"},
            },
            ["title", "start", "end"],
            risk=RiskLevel.MEDIUM,
        ),
    ),
)

# ── Notion (official @notionhq/notion-mcp-server) ──
NOTION_PACK = ConnectorPack(
    id="notion",
    name="Notion",
    version="1.0.0",
    # i18n: user-facing
    description="Notion 连接器：页面/数据库读取与写入（官方 notion-mcp-server）",
    transport="http",
    url="https://mcp.notion.com/mcp",
    env_credentials={"notion_token": "Authorization"},
    homepage="https://github.com/makenotion/notion-mcp-server",
    tools=(
        _tool(
            "search",
            # i18n: user-facing
            "搜索 Notion 工作区页面/数据库",
            {
                # i18n: user-facing
                "query": {"type": "string", "description": "搜索关键词"}
            },
            [],
            read_only=True,
        ),
        _tool(
            "fetch",
            # i18n: user-facing
            "读取 Notion 页面内容",
            {
                # i18n: user-facing
                "id": {"type": "string", "description": "页面/数据源 ID"}
            },
            ["id"],
            read_only=True,
        ),
        _tool(
            "create_pages",
            # i18n: user-facing
            "创建 Notion 页面",
            {
                # i18n: user-facing
                "parent": {"type": "object", "description": "父页面/数据库引用"},
                # i18n: user-facing
                "properties": {"type": "object", "description": "页面属性"},
            },
            ["parent"],
            risk=RiskLevel.HIGH,
        ),
    ),
)

# Builtin first-party catalog (id → pack)
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
    # Get a builtin first-party connector pack by ID.
    return FIRST_PARTY_PACKS.get(pack_id)


def list_builtin_packs() -> list[ConnectorPack]:
    # List all builtin first-party connector packs.
    return list(FIRST_PARTY_PACKS.values())
