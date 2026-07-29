"""
Bot de Telegram - Registro de producción CNC + Láser
------------------------------------------------------
Versión con base de datos PERSISTENTE (PostgreSQL en Supabase).
Los datos ya NO viven en el disco de Render, así que sobreviven a
cualquier redespliegue, reinicio o actualización del código.

Comandos:
  /venta      -> carga una venta paso a paso (conversación guiada)
  /resumen    -> resumen del mes actual (ingresos, costos, sueldo, utilidad)
  /mes <YYYY-MM> -> resumen de un mes específico, ej: /mes 2026-06
  /tarifas    -> ver o cambiar las tarifas de costo (ARS/min) y tu sueldo/hora
  /backup     -> descarga un CSV con todas las ventas cargadas
  /cancelar   -> cancela la carga en curso
"""

import os
import threading
import csv
import io
from datetime import datetime
import psycopg2
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

# ---------- Configuración ----------
TOKEN = os.environ.get("TELEGRAM_TOKEN", "PONÉ_TU_TOKEN_ACÁ")
DATABASE_URL = os.environ.get("DATABASE_URL", "")

MAQUINA, PRODUCTO, CANTIDAD, MINUTOS, MATERIAL, PRECIO = range(6)


# ---------- Servidor web mínimo (para que Render lo trate como Web Service gratuito) ----------
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot corriendo")

    def log_message(self, format, *args):
        pass


def start_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


# ---------- Base de datos (PostgreSQL persistente) ----------
def get_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS ventas (
            id SERIAL PRIMARY KEY,
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
    defaults = {
        "tarifa_cnc": 55, "tarifa_laser": 54, "sueldo_hora": 9500,
        "cnc_usd_min": 0.26, "laser_usd_min": 0.40, "sueldo_usd_hora": 6.15,
        "dolar_actual": 1545
    }
    for k, v in defaults.items():
        c.execute(
            "INSERT INTO config (clave, valor) VALUES (%s, %s) ON CONFLICT (clave) DO NOTHING",
            (k, v)
        )
    conn.commit()
    c.close()
    conn.close()


def get_config():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT clave, valor FROM config")
    rows = dict(c.fetchall())
    c.close()
    conn.close()
    return rows


def set_config(clave, valor):
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE config SET valor = %s WHERE clave = %s", (valor, clave))
    conn.commit()
    c.close()
    conn.close()


def guardar_venta(fecha, maquina, producto, cantidad, minutos, material, precio, usuario):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO ventas (fecha, maquina, producto, cantidad, minutos, material, precio, usuario)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """, (fecha, maquina, producto, cantidad, minutos, material, precio, usuario))
    conn.commit()
    c.close()
    conn.close()


def ventas_del_mes(anio_mes):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT fecha, maquina, producto, cantidad, minutos, material, precio
        FROM ventas WHERE fecha LIKE %s ORDER BY fecha DESC
    """, (anio_mes + "%",))
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows


def todas_las_ventas():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT fecha, maquina, producto, cantidad, minutos, material, precio, usuario
        FROM ventas ORDER BY fecha ASC
    """)
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows


def ultimas_ventas(cantidad=10):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT id, fecha, maquina, producto, cantidad, precio
        FROM ventas ORDER BY id DESC LIMIT %s
    """, (cantidad,))
    rows = c.fetchall()
    c.close()
    conn.close()
    return rows


def borrar_venta_por_id(venta_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM ventas WHERE id = %s", (venta_id,))
    existe = c.fetchone()
    if existe:
        c.execute("DELETE FROM ventas WHERE id = %s", (venta_id,))
        conn.commit()
    c.close()
    conn.close()
    return existe is not None



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
        f"✅ Venta guardada (base persistente).\n\n"
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


# ---------- Comando /ultimas ----------
async def ultimas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = ultimas_ventas(10)
    if not rows:
        await update.message.reply_text("No hay ventas cargadas todavía.")
        return

    lineas = ["🕐 Últimas 10 ventas:\n"]
    for venta_id, fecha, maquina, producto, cantidad, precio in rows:
        m = "CNC" if maquina == "cnc" else "Láser"
        lineas.append(f"#{venta_id} — {fecha} — {m} — {producto} ×{cantidad} — ${precio:,.0f}")
    lineas.append("\nPara borrar alguna: /borrar <número>, ej: /borrar 7")
    await update.message.reply_text("\n".join(lineas))


# ---------- Comando /borrar ----------
async def borrar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usá el formato: /borrar 7 (mirá el número con /ultimas)")
        return
    try:
        venta_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Mandame un número de venta válido, ej: /borrar 7")
        return

    ok = borrar_venta_por_id(venta_id)
    if ok:
        await update.message.reply_text(f"🗑️ Venta #{venta_id} borrada correctamente.")
    else:
        await update.message.reply_text(f"No encontré ninguna venta con el número #{venta_id}. Revisá con /ultimas.")



async def dolar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cfg = get_config()

    if not context.args:
        await update.message.reply_text(
            f"Dólar de referencia actual: ${cfg.get('dolar_actual', 0):.0f}\n\n"
            f"Tarifas base en USD (no cambian):\n"
            f"CNC: USD {cfg['cnc_usd_min']:.2f}/min\n"
            f"Láser: USD {cfg['laser_usd_min']:.2f}/min\n"
            f"Sueldo: USD {cfg['sueldo_usd_hora']:.2f}/hora\n\n"
            f"Para actualizar con el dólar de hoy: /dolar 1600\n"
            f"Para cambiar una tarifa base en USD: /basedolar cnc 0.26"
        )
        return

    try:
        valor_dolar = float(context.args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Mandame un número, ej: /dolar 1600")
        return

    nueva_cnc = cfg["cnc_usd_min"] * valor_dolar
    nueva_laser = cfg["laser_usd_min"] * valor_dolar
    nuevo_sueldo = cfg["sueldo_usd_hora"] * valor_dolar

    set_config("tarifa_cnc", nueva_cnc)
    set_config("tarifa_laser", nueva_laser)
    set_config("sueldo_hora", nuevo_sueldo)
    set_config("dolar_actual", valor_dolar)

    await update.message.reply_text(
        f"💵 Tarifas actualizadas con dólar a ${valor_dolar:.0f}\n\n"
        f"CNC: ${nueva_cnc:,.0f}/min (antes ${cfg['tarifa_cnc']:,.0f})\n"
        f"Láser: ${nueva_laser:,.0f}/min (antes ${cfg['tarifa_laser']:,.0f})\n"
        f"Sueldo: ${nuevo_sueldo:,.0f}/hora (antes ${cfg['sueldo_hora']:,.0f})\n\n"
        f"Las ventas nuevas que cargues van a usar estos valores."
    )


# ---------- Comando /basedolar (para ajustar las tarifas base en USD, poco frecuente) ----------
async def basedolar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.message.reply_text(
            "Formato: /basedolar cnc 0.26  |  /basedolar laser 0.40  |  /basedolar sueldo 6.15\n\n"
            "Esto cambia la tarifa BASE en dólares (la referencia que se usa con /dolar), "
            "no la tarifa en pesos directamente."
        )
        return
    clave_map = {"cnc": "cnc_usd_min", "laser": "laser_usd_min", "sueldo": "sueldo_usd_hora"}
    clave = clave_map.get(context.args[0].lower())
    if not clave:
        await update.message.reply_text("Usá: cnc, laser o sueldo")
        return
    try:
        valor = float(context.args[1].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Mandame un número válido, ej: 0.26")
        return
    set_config(clave, valor)
    await update.message.reply_text(
        f"Listo, base en USD de {context.args[0]} actualizada a {valor}.\n"
        f"Usá /dolar <valor> para recalcular las tarifas en pesos con este nuevo valor base."
    )



async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = todas_las_ventas()
    if not rows:
        await update.message.reply_text("No hay ventas cargadas todavía.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["fecha", "maquina", "producto", "cantidad", "minutos", "material", "precio", "usuario"])
    writer.writerows(rows)

    data = io.BytesIO(output.getvalue().encode("utf-8"))
    data.name = f"backup-produccion-{datetime.now().strftime('%Y-%m-%d')}.csv"

    await update.message.reply_document(
        document=data,
        filename=data.name,
        caption=f"📦 Backup completo — {len(rows)} venta(s) cargada(s)."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Registro de producción CNC + Láser\n\n"
        "/venta - cargar una venta nueva\n"
        "/resumen - ver el mes actual\n"
        "/mes 2026-06 - ver un mes específico\n"
        "/tarifas - ver o cambiar tarifas y sueldo\n"
        "/dolar 1600 - recalcular tarifas y sueldo según el dólar de hoy\n"
        "/ultimas - ver las últimas 10 ventas con su número\n"
        "/borrar 7 - borrar una venta cargada por error\n"
        "/backup - descargar todas las ventas en CSV"
    )


def main():
    init_db()

    threading.Thread(target=start_web_server, daemon=True).start()

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
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("dolar", dolar))
    app.add_handler(CommandHandler("basedolar", basedolar))
    app.add_handler(CommandHandler("ultimas", ultimas))
    app.add_handler(CommandHandler("borrar", borrar))

    print("Bot corriendo (base de datos persistente)...")
    app.run_polling()


if __name__ == "__main__":
    main()
