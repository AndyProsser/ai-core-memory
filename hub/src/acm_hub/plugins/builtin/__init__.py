from .apprise_sink import ApprisePlugin
from .memos import MemosPlugin
from .obsidian import ObsidianPlugin
from .webhook import WebhookPlugin

BUILTINS = [ApprisePlugin(), WebhookPlugin(), ObsidianPlugin(), MemosPlugin()]
