from .apprise_sink import ApprisePlugin
from .joplin import JoplinPlugin
from .memos import MemosPlugin
from .obsidian import ObsidianPlugin
from .obsidian_rest import ObsidianRestPlugin
from .webhook import WebhookPlugin

BUILTINS = [
    ApprisePlugin(),
    WebhookPlugin(),
    ObsidianPlugin(),
    MemosPlugin(),
    JoplinPlugin(),
    ObsidianRestPlugin(),
]
