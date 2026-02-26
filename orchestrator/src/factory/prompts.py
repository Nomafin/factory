import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PRIVATE_DIR = "private"


def load_prompt(system_prompt_file: str, base_dir: Path) -> str:
    """Load a system prompt, combining public and private prompt files.

    Public prompts live in the repo (e.g. prompts/coder.md).
    Private prompts live in prompts/private/ (gitignored) and are
    appended to the public prompt when present.

    Args:
        system_prompt_file: Relative path to the public prompt file
                            (e.g. "prompts/coder.md").
        base_dir: Project root directory that paths are resolved against.

    Returns:
        Combined prompt text, or empty string if no files found.
    """
    if not system_prompt_file:
        return ""

    parts: list[str] = []

    # Load public prompt
    public_path = base_dir / system_prompt_file
    if public_path.is_file():
        content = public_path.read_text().strip()
        if content:
            parts.append(content)
    else:
        logger.warning("Public prompt file not found: %s", public_path)

    # Derive private prompt path: prompts/coder.md -> prompts/private/coder.md
    prompt_path = Path(system_prompt_file)
    if len(prompt_path.parts) >= 2:
        private_path = base_dir / prompt_path.parts[0] / PRIVATE_DIR / Path(*prompt_path.parts[1:])
    else:
        private_path = base_dir / PRIVATE_DIR / prompt_path

    if private_path.is_file():
        content = private_path.read_text().strip()
        if content:
            parts.append(content)
        logger.debug("Loaded private prompt from %s", private_path)

    return "\n\n".join(parts)
