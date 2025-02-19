import click

from .kb_add_local_dir import add_local_dir
from .kb_add_search import add_search
from .kb_add_url import add_url
from .kb_add_url_list import add_url_list
from .kb_crud import create
from .kb_list import list
from .kb_list_db import list_db


@click.group()
def kb():
    """
    Knowledge base management.
    """
    pass


kb.add_command(list)
kb.add_command(list_db)
kb.add_command(add_local_dir)
kb.add_command(add_search)
kb.add_command(add_url_list)
kb.add_command(add_url)
kb.add_command(create)
