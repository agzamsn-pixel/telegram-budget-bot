import os
import json
import base64
import asyncio
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN')
ANTHROPIC_API_KEY = os.getenv('ANTHROPIC_API_KEY')
GOOGLE_SHEETS_ID  = os.getenv('GOOGLE_SHEETS_ID')
# Через запятую: твой Telegram user_id (узнать через @userinfobot)
ALLOWED_IDS = [int(x) for x in os.getenv('ALLOWED_USER_IDS', '').split(',') if x.strip()]

CATEGORIES = [
    '🏠 Аренда', '💡 Коммуналка', '🛒 Продукты',
    '📚 Образование', '🚗 Транспорт', '💊 Здоровье',
    '📱 Подписки', '🍽 Кафе', '👗 Одежда',
    '🎮 Развлечения', '❓ Другое',
]

# ── Google Sheets ────────────────────────────────────────────────
def get_worksheet():
    scope = [
        'https://spreadsheets.google.com/feeds',
        'https://www.googleapis.com/auth/drive'
    ]
    creds_json = os.getenv('GOOGLE_CREDENTIALS')
    if creds_json:
        import json as _json
        creds = Credentials.from_service_account_info(_json.loads(creds_json), scopes=scope)
    else:
        creds = Credentials.from_service_account_file('credentials.json', scopes=scope)
    client = gspread.authorize(creds)
    sh = client.open_by_key(GOOGLE_SHEETS_ID)
    try:
        ws = sh.worksheet('Расходы')
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title='Расходы', rows=1000, cols=6)
        ws.append_row(['Дата', 'Магазин / Описание', 'Сумма', 'Категория', 'Комментарий', 'Добавлено'])
    return ws

def sheet_add(date, store, amount, category, comment=''):
    ws = get_worksheet()
    ws.append_row([
        date, store, float(amount), category, comment,
        datetime.now().strftime('%Y-%m-%d %H:%M')
    ])

def sheet_monthly_summary():
    ws = get_worksheet()
    records = ws.get_all_records()
    current_month = datetime.now().strftime('%Y-%m')
    rows = [r for r in records if str(r.get('Дата', '')).startswith(current_month)]
    total = sum(float(r.get('Сумма', 0)) for r in rows)
    by_cat = {}
    for r in rows:
        cat = r.get('Категория', 'Другое')
        by_cat[cat] = by_cat.get(cat, 0) + float(r.get('Сумма', 0))
    return total, by_cat, len(rows)

# ── Claude: читает чек ───────────────────────────────────────────
def read_receipt_with_claude(image_bytes: bytes) -> dict | None:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    b64 = base64.standard_b64encode(image_bytes).decode('utf-8')

    resp = client.messages.create(
        model='claude-haiku-4-5-20251001',
        max_tokens=512,
        messages=[{
            'role': 'user',
            'content': [
                {
                    'type': 'image',
                    'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': b64}
                },
                {
                    'type': 'text',
                    'text': (
                        'Прочитай чек / квитанцию и верни ТОЛЬКО JSON (без markdown):\n'
                        '{\n'
                        '  "store": "название магазина или организации",\n'
                        '  "date": "YYYY-MM-DD (если видна, иначе сегодняшняя дата)",\n'
                        '  "amount": 0.0,\n'
                        '  "category": "одно из: Продукты, Кафе, Транспорт, Здоровье, Одежда, Коммуналка, Образование, Развлечения, Другое",\n'
                        '  "comment": "краткое описание (необязательно)"\n'
                        '}\n'
                        'Сумма — итоговая к оплате. Только JSON, ничего лишнего.'
                    )
                }
            ]
        }]
    )

    raw = resp.content[0].text.strip()
    # убираем ```json ... ``` если есть
    if '```' in raw:
        parts = raw.split('```')
        raw = parts[1].lstrip('json').strip() if len(parts) > 1 else raw
    try:
        return json.loads(raw)
    except Exception:
        return None

# ── Helpers ──────────────────────────────────────────────────────
def fmt(n):
    return f"{float(n):,.2f}"

def is_allowed(update: Update) -> bool:
    if not ALLOWED_IDS:
        return True
    return update.effective_user.id in ALLOWED_IDS

def expense_text(data: dict) -> str:
    return (
        f"🏪 *{data.get('store', '?')}*\n"
        f"💰 {fmt(data.get('amount', 0))} €\n"
        f"📂 {data.get('category', 'Другое')}\n"
        f"📅 {data.get('date', datetime.now().strftime('%Y-%m-%d'))}"
        + (f"\n💬 {data['comment']}" if data.get('comment') else '')
    )

def confirm_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Добавить', callback_data='ok'),
         InlineKeyboardButton('✏️ Категория', callback_data='cats')],
        [InlineKeyboardButton('❌ Отменить', callback_data='no')],
    ])

# ── Handlers ─────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        '👋 *Трекер расходов*\n\n'
        '📸 Отправь фото чека — прочитаю и запишу\n'
        '✏️ Или напиши: `Магнум 12500`\n'
        '📊 /summary — итоги месяца\n'
        '📋 /last — последние 5 записей',
        parse_mode='Markdown'
    )

async def cmd_summary(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text('⏳ Загружаю...')
    try:
        total, by_cat, count = sheet_monthly_summary()
        month_name = datetime.now().strftime('%B %Y')
        lines = [f'📊 *{month_name}* — {count} записей\n']
        for cat, amt in sorted(by_cat.items(), key=lambda x: -x[1]):
            bar = '█' * int(amt / total * 10) if total else ''
            lines.append(f'{cat}: *{fmt(amt)} €* {bar}')
        lines.append(f'\n💰 *Итого: {fmt(total)} €*')
        await msg.edit_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f'❌ {e}')

async def cmd_last(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    try:
        ws = get_worksheet()
        rows = ws.get_all_records()
        last = rows[-5:] if len(rows) >= 5 else rows
        last.reverse()
        lines = ['📋 *Последние записи:*\n']
        for r in last:
            lines.append(f"• {r['Магазин / Описание']} — *{fmt(r['Сумма'])} €* ({r['Категория']})")
        await update.message.reply_text('\n'.join(lines), parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f'❌ {e}')

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    msg = await update.message.reply_text('🔍 Читаю чек...')
    try:
        photo = update.message.photo[-1]
        file = await ctx.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        data = read_receipt_with_claude(bytes(image_bytes))
        if not data or not data.get('amount'):
            await msg.edit_text('❌ Не смог прочитать чек. Попробуй ещё раз или введи вручную:\n`Название сумма`', parse_mode='Markdown')
            return
        # Ставим сегодняшнюю дату если не распознана
        if not data.get('date'):
            data['date'] = datetime.now().strftime('%Y-%m-%d')
        ctx.user_data['pending'] = data
        await msg.edit_text(expense_text(data) + '\n\nДобавить?', reply_markup=confirm_keyboard(), parse_mode='Markdown')
    except Exception as e:
        await msg.edit_text(f'❌ Ошибка: {e}')

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    text = update.message.text.strip()
    parts = text.rsplit(None, 1)
    if len(parts) == 2:
        try:
            amount = float(parts[1].replace(',', '').replace(' ', ''))
            data = {
                'store': parts[0],
                'amount': amount,
                'date': datetime.now().strftime('%Y-%m-%d'),
                'category': 'Другое',
                'comment': ''
            }
            ctx.user_data['pending'] = data
            await update.message.reply_text(expense_text(data) + '\n\nДобавить?', reply_markup=confirm_keyboard(), parse_mode='Markdown')
            return
        except ValueError:
            pass
    await update.message.reply_text('📸 Пришли фото чека\nили напиши: `Магнум 12500`', parse_mode='Markdown')

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == 'ok':
        data = ctx.user_data.get('pending')
        if not data:
            await q.edit_message_text('❌ Нет данных для сохранения.')
            return
        try:
            sheet_add(data['date'], data['store'], data['amount'], data['category'], data.get('comment', ''))
            await q.edit_message_text(f'✅ Записано: *{data["store"]}* — {fmt(data["amount"])} €', parse_mode='Markdown')
            ctx.user_data.pop('pending', None)
        except Exception as e:
            await q.edit_message_text(f'❌ Ошибка записи: {e}')

    elif q.data == 'no':
        ctx.user_data.pop('pending', None)
        await q.edit_message_text('❌ Отменено')

    elif q.data == 'cats':
        buttons = []
        row = []
        for i, cat in enumerate(CATEGORIES):
            row.append(InlineKeyboardButton(cat, callback_data=f'c_{i}'))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        await q.edit_message_text('Выбери категорию:', reply_markup=InlineKeyboardMarkup(buttons))

    elif q.data.startswith('c_'):
        idx = int(q.data[2:])
        cat_full = CATEGORIES[idx]
        # убираем эмодзи
        cat = cat_full.split(' ', 1)[1] if ' ' in cat_full else cat_full
        if 'pending' in ctx.user_data:
            ctx.user_data['pending']['category'] = cat
            data = ctx.user_data['pending']
            await q.edit_message_text(expense_text(data) + '\n\nДобавить?', reply_markup=confirm_keyboard(), parse_mode='Markdown')

# ── Main ─────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler('start', cmd_start))
    app.add_handler(CommandHandler('summary', cmd_summary))
    app.add_handler(CommandHandler('last', cmd_last))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print('✅ Бот запущен')
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
