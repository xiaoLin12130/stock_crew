import json
import os
from pathlib import Path

# Skill 文件存放目录（项目根目录下的 skills/）
SKILLS_DIR = Path(__file__).parent.parent.parent.parent / "skills"

def list_skills() -> list[dict]:
    if not SKILLS_DIR:
        return []
    skills = []
    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "skill.json"
        if not skill_file.exists():
            continue
        with open(skill_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["id"] = skill_dir.name
        skills.append(data)
    return skills

def load_skill(skill_id: str) -> dict | None:
    skill_file = SKILLS_DIR / skill_id / "skill.json"
    if not skill_file.exists():
        return None
    with open(skill_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    data["id"] = skill_id
    return data