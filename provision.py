"""
Разворачивание нового клиента на сервере.

Создаёт папку, пишет `.env`, запускает службу. Ровно то, что оператор
делал бы руками по `СЕРВЕР.md`, — только по кнопке и без опечаток.

## Что нужно разрешить на сервере

Боту нельзя давать полный sudo. Достаточно права запускать службы своего
шаблона. Один файл:

    sudo tee /etc/sudoers.d/fixbot > /dev/null <<'EOF'
    fixbot ALL=(root) NOPASSWD: /bin/systemctl enable --now fixbot@*, \\
                                /bin/systemctl restart fixbot@*, \\
                                /bin/systemctl stop fixbot@*, \\
                                /bin/systemctl is-active fixbot@*
    EOF
    sudo chmod 440 /etc/sudoers.d/fixbot

Замените `fixbot` на пользователя, под которым работает бот. Больше
никаких прав не требуется: ни установки пакетов, ни доступа к чужим
файлам.

## Если systemd нет

На маке служб нет, и это нормально: тогда `deploy` только создаст папку
с настройками и честно сообщит, что запускать нужно вручную. Проверить
всю цепочку до этого места можно и локально.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

#: Настройки, которые наследуются от оператора: они одинаковы у всех
#: клиентов, и заставлять человека их вводить незачем.
INHERITED = (
    "ANTHROPIC_API_KEY", "MODEL", "MIN_CONFIDENCE", "CONFIRM_TTL_MIN",
    "STATUS_CHECK_MIN", "FIXATION_TTL_DAYS", "RENEW_WARN_DAYS",
    "SYNC_INTERVAL_MIN", "LIVE_LOOKUP", "RETAIL_TTL_DAYS",
    "DEFAULT_REGION", "DEFAULT_LANG", "QUIET", "SHOW_CRM_LINKS",
)

ENV_TEMPLATE = """# {developer} — создано помощником подключения {stamp}

TELEGRAM_TOKEN={bot_token}
AMO_SUBDOMAIN={subdomain}
AMO_AUTH=long_lived
AMO_LONG_TOKEN={amo_token}
DEVELOPER_NAME={developer}

# Оператор — техническое. Владелец — рассылки, группы, статистика.
OPERATOR_IDS={operator_ids}
OWNER_IDS={owner_ids}

DB_PATH={db_path}

# Первую неделю в режиме наблюдения: бот отвечает в чатах, но в CRM
# ничего не пишет. Так ловятся промахи распознавания без мусора в базе.
DRY_RUN=1

{inherited}
"""


def render_env(*, developer: str, bot_token: str, subdomain: str,
               amo_token: str, operator_ids: set[int] | list[int],
               owner_ids: set[int] | list[int], db_path: str,
               inherited: dict[str, str] | None = None,
               stamp: str = "") -> str:
    lines = [f"{k}={v}" for k, v in (inherited or {}).items() if v != ""]
    return ENV_TEMPLATE.format(
        developer=developer, stamp=stamp, bot_token=bot_token,
        subdomain=subdomain, amo_token=amo_token,
        operator_ids=",".join(str(x) for x in sorted(operator_ids)),
        owner_ids=",".join(str(x) for x in sorted(owner_ids)),
        db_path=db_path, inherited="\n".join(lines),
    )


def collect_inherited(env: dict[str, str]) -> dict[str, str]:
    """Берёт из окружения оператора то, что общее для всех клиентов."""
    return {k: env[k] for k in INHERITED if env.get(k)}


class ProvisionError(RuntimeError):
    pass


async def _run(*args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode(errors="replace").strip()


def has_systemd() -> bool:
    return shutil.which("systemctl") is not None


async def deploy(*, clients_dir: str, slug: str, env_text: str,
                 service: str = "fixbot") -> dict:
    """
    Создаёт клиента и пытается запустить его службу.

    Возвращает отчёт: что сделано и что осталось руками. Ошибку запуска
    не считаем провалом всего — настройки уже на месте, и оператор может
    поднять службу сам.
    """
    root = Path(clients_dir).expanduser()
    if not clients_dir:
        raise ProvisionError("не задана папка клиентов (CLIENTS_DIR)")

    folder = root / slug
    if (folder / ".env").exists():
        raise ProvisionError(f"клиент «{slug}» уже существует")

    try:
        folder.mkdir(parents=True, exist_ok=True)
        env_file = folder / ".env"
        env_file.write_text(env_text, encoding="utf-8")
        # В файле лежат токены — читать его должен только владелец процесса.
        env_file.chmod(0o600)
    except OSError as e:
        raise ProvisionError(f"не смог создать папку: {e}") from e

    report = {"folder": str(folder), "started": False, "log": ""}

    if not has_systemd():
        report["log"] = ("systemd не найден — вероятно это не сервер. "
                         "Настройки созданы, службу запустите вручную.")
        return report

    code, out = await _run("sudo", "-n", "systemctl", "enable", "--now",
                           f"{service}@{slug}")
    report["log"] = out
    if code != 0:
        report["log"] = (out or "systemctl вернул ошибку") + \
            "\n\nПроверьте права в /etc/sudoers.d/fixbot."
        return report

    await asyncio.sleep(3)
    code, state = await _run("sudo", "-n", "systemctl", "is-active",
                             f"{service}@{slug}")
    report["started"] = state.strip() == "active"
    report["log"] = state.strip() or report["log"]
    return report


def deploy_report(slug: str, report: dict, bot_handle: str | None) -> str:
    lines = ["🚀 <b>Клиент развёрнут</b>", "",
             f"Папка: <code>{report['folder']}</code>"]
    if report["started"]:
        lines.append(f"Служба: <code>fixbot@{slug}</code> — работает")
    else:
        lines += ["Служба: ❗️ не запустилась",
                  f"<i>{report['log'][:300]}</i>", "",
                  "Настройки на месте, запустить можно вручную:",
                  f"<code>sudo systemctl enable --now fixbot@{slug}</code>"]
    lines += ["", "Дальше: разметьте воронки в боте клиента "
                  "(«🔧 Техническое» → «Разметка воронок») и проверьте "
                  "<code>/check</code> на живом номере."]
    if bot_handle:
        lines += ["", f"Бот клиента: {bot_handle}"]
    return "\n".join(lines)
