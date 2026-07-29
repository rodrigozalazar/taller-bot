"""
Bot de Telegram - Registro de producción CNC + Láser
------------------------------------------------------
Guarda cada venta en una base de datos SQLite en el servidor.
Como los datos viven en el servidor (no en tu celular/PC), se ven
sincronizados automáticamente sin importar desde qué dispositivo entrás
a Telegram.

Comandos:
  /venta      -> carga una venta paso a paso (conversación guiada)
  /resumen    -> resumen del mes actual (ingresos, costos, sueldo, utilidad)
  /mes <YYYY-MM> -> resumen de un mes específico, ej: /mes 2026-06
  /tarifas    -> ver o cambiar las tarifas de costo (ARS/min) y tu sueldo/hora
  /cancelar   -> cancela la carga en curso
"""

import os
import sqlite3
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ---------- Configuración ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN", "PONÉ_TU_TOKEN_ACÁ")
DB_PATH = os.environ.get("DB_PATH", "produccion.db")

MAQUINA, PRODUCTO, CANTIDAD, MINUTOS, MATERIAL, PRECIO = range(6)


# ---------- Base de datos ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ventas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT NOT NULL,
            maquina TEXT NOT NULL,
            producto TEXT NOT NULL,
            cantidad INTEGER NOT NULL,
            minutos REAL NOT NULL,
            material REAL NOT NULL,
            precio REAL NOT NULL,
            usuario TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor REAL NOT NULL
        )
    """)
    # Valores por defecto (los mismos que ya veníamos usando)
    defaults = {"tarifa_cnc": 55, "tarifa_laser": 54, "sueldo_hora": 9500}
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO config (clave, valor) VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


def get_config():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT clave, valor FROM config")
    rows = dict(c.fetchall())
    conn.close()
    return rows


def set_config(clave, valor):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE config SET valor = ? WHERE clave = ?", (valor, clave))
    conn.commit()
    conn.close()


def guardar_venta(fecha, maquina, producto, cantidad, minutos, material, precio, usuario):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO ventas (fecha, maquina, producto, cantidad, minutos, material, precio, usuario)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (fecha, maquina, producto, cantidad, minutos, material, precio, usuario))
    conn.commit()
    conn.close()


def ventas_del_mes(anio_mes):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT fecha, maquina, producto, cantidad, minutos, material, precio
        FROM ventas WHERE fecha LIKE ? ORDER BY fecha DESC
    """, (anio_mes + "%",))
    rows = c.fetchall()
    conn.close()
    return rows


# ---------- Comando /venta (conversación guiada) ----------
async def venta_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = ReplyKeyboardMarkup([["CNC", "Láser"]], one_time_keyboard=True, resize_keyboard=True)
    await update.message.reply_text("¿Qué máquina usaste?", reply_markup=keyboard)
    return MAQUINA


async def venta_maquina(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().lower()
    context.user_data["maquina"] = "cnc" if "cnc" in texto else "laser"
    await update.message.reply_text("¿Qué producto o para qué cliente? (texto libre)", reply_markup=ReplyKeyboardRemove())
    return PRODUCTO


async def venta_producto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["producto"] = update.message.text.strip()
    await update.message.reply_text("¿Cantidad de piezas?")
    return CANTIDAD


async def venta_cantidad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["cantidad"] = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("Mandame un número entero, ej: 20")
        return CANTIDAD
    await update.message.reply_text("¿Cuántos minutos totales de máquina llevó?")
    return MINUTOS


async def venta_minutos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["minutos"] = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        await update.message.reply_text("Mandame un número, ej: 45 o 45.5")
        return MINUTOS
    await update.message.reply_text("¿Costo de material total? (0 si no aplica)")
    return MATERIAL


async def venta_material(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["material"] = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        await update.message.reply_text("Mandame un número, ej: 3200")
        return MATERIAL
    await update.message.reply_text("¿Precio total cobrado?")
    return PRECIO


async def venta_precio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        precio = float(update.message.text.strip().replace(",", "."))
    except ValueError:
        await update.message.reply_text("Mandame un número, ej: 22000")
        return PRECIO

    d = context.user_data
    fecha = datetime.now().strftime("%Y-%m-%d")
    usuario = update.effective_user.first_name or "desconocido"
    guardar_venta(fecha, d["maquina"], d["producto"], d["cantidad"],
                  d["minutos"], d["material"], precio, usuario)

    cfg = get_config()
    tarifa = cfg["tarifa_cnc"] if d["maquina"] == "cnc" else cfg["tarifa_laser"]
    costo = d["minutos"] * tarifa + d["material"]
    sueldo = (d["minutos"] / 60) * cfg["sueldo_hora"]
    utilidad = precio - costo - sueldo

    await update.message.reply_text(
        f"✅ Venta guardada.\n\n"
        f"Producto: {d['producto']} ×{d['cantidad']}\n"
        f"Máquina: {'CNC' if d['maquina']=='cnc' else 'Láser'}\n"
        f"Precio: ${precio:,.0f}\n"
        f"Costo real: ${costo:,.0f}\n"
        f"Tu sueldo (por el tiempo): ${sueldo:,.0f}\n"
        f"Utilidad del negocio: ${utilidad:,.0f}\n\n"
        f"Usá /resumen para ver el total del mes."
    )
    return ConversationHandler.END


async def venta_cancelar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Carga cancelada.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ---------- Comando /resumen ----------
def calcular_resumen(anio_mes):
    cfg = get_config()
    rows = ventas_del_mes(anio_mes)
    ingresos = costos = sueldo_total = 0
    gan_cnc = gan_laser = 0
    for fecha, maquina, producto, cantidad, minutos, material, precio in rows:
        tarifa = cfg["tarifa_cnc"] if maquina == "cnc" else cfg["tarifa_laser"]
        costo = minutos * tarifa + material
        sueldo = (minutos / 60) * cfg["sueldo_hora"]
        ingresos += precio
        costos += costo
        sueldo_total += sueldo
        ganancia = precio - costo
        if maquina == "cnc":
            gan_cnc += ganancia
        else:
            gan_laser += ganancia
    utilidad = ingresos - costos - sueldo_total
    return {
        "ventas": len(rows), "ingresos": ingresos, "costos": costos,
        "sueldo": sueldo_total, "utilidad": utilidad,
        "gan_cnc": gan_cnc, "gan_laser": gan_laser
    }


async def resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    anio_mes = datetime.now().strftime("%Y-%m")
    r = calcular_resumen(anio_mes)
    await update.message.reply_text(
        f"📊 Resumen de {anio_mes}\n\n"
        f"Ventas cargadas: {r['ventas']}\n"
        f"Ingresos: ${r['ingresos']:,.0f}\n"
        f"Costos reales: ${r['costos']:,.0f}\n"
        f"Tu sueldo: ${r['sueldo']:,.0f}\n"
        f"Utilidad del negocio: ${r['utilidad']:,.0f}\n\n"
        f"— CNC: ${r['gan_cnc']:,.0f}\n"
        f"— Láser: ${r['gan_laser']:,.0f}"
    )


async def resumen_mes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usá el formato: /mes 2026-06")
        return
    anio_mes = context.args[0]
    r = calcular_resumen(anio_mes)
    await update.message.reply_text(
        f"📊 Resumen de {anio_mes}\n\n"
        f"Ventas cargadas: {r['ventas']}\n"
        f"Ingresos: ${r['ingresos']:,.0f}\n"
        f"Costos reales: ${r['costos']:,.0f}\n"
        f"Tu sueldo: ${r['sueldo']:,.0f}\n"
        f"Utilidad del negocio: ${r['utilidad']:,.0f}\n\n"
        f"— CNC: ${r['gan_cnc']:,.0f}\n"
        f"— Láser: ${r['gan_laser']:,.0f}"
    )


# ---------- Comando /tarifas ----------
async def tarifas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cfg = get_config()
        await update.message.reply_text(
            f"Tarifas actuales:\n"
            f"CNC: ${cfg['tarifa_cnc']:.0f}/min\n"
            f"Láser: ${cfg['tarifa_laser']:.0f}/min\n"
            f"Sueldo objetivo: ${cfg['sueldo_hora']:.0f}/hora\n\n"
            f"Para cambiar: /tarifas cnc 55  |  /tarifas laser 54  |  /tarifas sueldo 9500"
        )
        return
    if len(context.args) != 2:
        await update.message.reply_text("Formato: /tarifas cnc 55")
        return
    clave_map = {"cnc": "tarifa_cnc", "laser": "tarifa_laser", "sueldo": "sueldo_hora"}
    clave = clave_map.get(context.args[0].lower())
    if not clave:
        await update.message.reply_text("Usá: cnc, laser o sueldo")
        return
    try:
        valor = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Mandame un número válido")
        return
    set_config(clave, valor)
    await update.message.reply_text(f"Listo, {context.args[0]} actualizado a {valor}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Registro de producción CNC + Láser\n\n"
        "/venta - cargar una venta nueva\n"
        "/resumen - ver el mes actual\n"
        "/mes 2026-06 - ver un mes específico\n"
        "/tarifas - ver o cambiar tarifas y sueldo"
    )


def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("venta", venta_start)],
        states={
            MAQUINA: [MessageHandler(filters.TEXT & ~filters.COMMAND, venta_maquina)],
            PRODUCTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, venta_producto)],
            CANTIDAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, venta_cantidad)],
            MINUTOS: [MessageHandler(filters.TEXT & ~filters.COMMAND, venta_minutos)],
            MATERIAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, venta_material)],
            PRECIO: [MessageHandler(filters.TEXT & ~filters.COMMAND, venta_precio)],
        },
        fallbacks=[CommandHandler("cancelar", venta_cancelar)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("resumen", resumen))
    app.add_handler(CommandHandler("mes", resumen_mes))
    app.add_handler(CommandHandler("tarifas", tarifas))

    print("Bot corriendo...")
    app.run_polling()


if __name__ == "__main__":
    main()
