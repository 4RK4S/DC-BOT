import nextcord


DEFAULT_COLOR = nextcord.Color.from_rgb(55, 118, 255)


def admin_panel_embed() -> nextcord.Embed:
    embed = nextcord.Embed(
        title="Admin Panel",
        description="Choose a module to configure.",
        color=DEFAULT_COLOR,
    )
    embed.add_field(name="Navigation", value="Navigation edits this panel message.", inline=False)
    embed.set_footer(text="Modules are placeholders until implemented.")
    return embed


def info_embed(title: str, description: str) -> nextcord.Embed:
    return nextcord.Embed(title=title, description=description, color=DEFAULT_COLOR)
