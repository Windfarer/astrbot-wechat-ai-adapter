from astrbot.api.star import Context, Star


class Main(Star):
    def __init__(self, context: Context) -> None:
        super().__init__(context)
        try:
            from .wechat_ai_platform_adapter import WechatAIPlatformAdapter  # noqa: F401
        except ImportError:
            from wechat_ai_platform_adapter import WechatAIPlatformAdapter  # noqa: F401
