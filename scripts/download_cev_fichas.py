import argparse
import re
import sys
import time
import math
import requests
import pandas as pd

from pathlib import Path
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import NoAlertPresentException

from webdriver_manager.chrome import ChromeDriverManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from cev_analisis.paths import default_paths_config, load_paths, path_value


# ============================================================
# CONFIGURACIÓN
# ============================================================

DEFAULT_PATHS = load_paths(default_paths_config(Path(__file__).resolve()))

BASE_DIR = path_value(DEFAULT_PATHS, "cev_pdf_dir", Path("/home/cdo/Downloads/CEV_fichas"))
BASE_DIR.mkdir(parents=True, exist_ok=True)

LOG_CSV = path_value(DEFAULT_PATHS, "download_log_csv_path", BASE_DIR / "descargas_cev_log.csv")
LOG_CSV.parent.mkdir(parents=True, exist_ok=True)

URL = "https://calificacionenergeticaweb.minvu.cl/Publico/BusquedaVivienda.aspx"

ID_REGION = "ContentPlaceHolder1_dbRegion"
ID_COMUNA = "ContentPlaceHolder1_dbComuna"
ID_BOTON = "ContentPlaceHolder1_BtnConsultarbusq"

# True: descarga solo TARGET_COMUNA
# False: descarga todas las comunas disponibles en el sitio
ONLY_ONE_COMUNA = False

TARGET_REGION_CONTAINS = "Metropolitana"
TARGET_COMUNA = "La Cisterna"

HEADLESS = True

PAGE_TIMEOUT = 120
REQUEST_TIMEOUT = 240

MAX_RETRIES_DOWNLOAD = 4
MAX_RETRIES_PAGE = 4

SLEEP_RETRY_BASE = 8
PAUSA_ENTRE_DESCARGAS = 1.5
PAUSA_ENTRE_PAGINAS = 1.5
PAUSA_ENTRE_COMUNAS = 3.0

SECCIONES = ["precalificadas", "calificadas"]

FILAS_POR_PAGINA = 10

# True: descarga solo "Ver Informe".
# False: descarga informes y etiquetas.
SOLO_INFORMES = True


# ============================================================
# UTILIDADES
# ============================================================

def limpiar_nombre_archivo(texto, max_len=180):
    texto = str(texto).lower().strip()

    reemplazos = {
        "á": "a", "é": "e", "í": "i", "ó": "o", "ú": "u",
        "ñ": "n", "ü": "u"
    }

    for k, v in reemplazos.items():
        texto = texto.replace(k, v)

    texto = re.sub(r"[\/\\\:\*\?\"\<\>\|]", "_", texto)
    texto = re.sub(r"[^a-z0-9\-_\. ]+", "", texto)
    texto = re.sub(r"\s+", "_", texto)
    texto = re.sub(r"_+", "_", texto)
    texto = texto.strip("._-")

    if not texto:
        texto = "ficha_sin_identificacion"

    return texto[:max_len]


def carpeta_comuna(comuna_name):
    nombre = limpiar_nombre_archivo(comuna_name, max_len=120)
    path = BASE_DIR / nombre
    path.mkdir(parents=True, exist_ok=True)
    return path


def guardar_log(fila):
    df = pd.DataFrame([fila])

    if LOG_CSV.exists():
        df.to_csv(LOG_CSV, mode="a", header=False, index=False, encoding="utf-8-sig")
    else:
        df.to_csv(LOG_CSV, mode="w", header=True, index=False, encoding="utf-8-sig")


def cargar_log_ok():
    if not LOG_CSV.exists():
        return set()

    df = pd.read_csv(LOG_CSV, dtype=str)

    if df.empty or "estado" not in df.columns:
        return set()

    df_ok = df[df["estado"].astype(str) == "ok"].copy()

    claves = set()

    for _, r in df_ok.iterrows():
        region = str(r.get("region", ""))
        comuna = str(r.get("comuna", ""))
        seccion = str(r.get("seccion", ""))
        identificacion = str(r.get("identificacion", ""))

        accion_title = str(r.get("accion_title", ""))
        accion_name = str(r.get("accion_name", ""))

        if accion_title and accion_title.lower() != "nan":
            claves.add((region, comuna, seccion, identificacion, accion_title))

        if accion_name and accion_name.lower() != "nan":
            claves.add((region, comuna, seccion, identificacion, accion_name))

    return claves


def ya_descargado(claves_ok, region, comuna, seccion, identificacion, accion_title, accion_name=None):
    claves = [
        (
            str(region),
            str(comuna),
            str(seccion),
            str(identificacion),
            str(accion_title),
        )
    ]

    if accion_name:
        claves.append((
            str(region),
            str(comuna),
            str(seccion),
            str(identificacion),
            str(accion_name),
        ))

    return any(clave in claves_ok for clave in claves)


def guardar_bytes_descargados(content, nombre_base, content_type="", output_dir=None):
    if output_dir is None:
        output_dir = BASE_DIR

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if content.lstrip().startswith(b"%PDF") or "pdf" in content_type.lower():
        ext = ".pdf"
    else:
        ext = ".bin"

    destino = output_dir / f"{nombre_base}{ext}"

    i = 2
    while destino.exists():
        destino = output_dir / f"{nombre_base}_{i}{ext}"
        i += 1

    destino.write_bytes(content)
    return destino


def guardar_debug_respuesta(nombre_base, response, output_dir=None):
    if output_dir is None:
        output_dir = BASE_DIR

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    debug_path = output_dir / f"{nombre_base}_respuesta_debug.html"

    try:
        debug_path.write_text(response.text, encoding="utf-8", errors="ignore")
    except Exception:
        debug_path.write_bytes(response.content)

    return debug_path


def pdf_ya_existe(nombre_base, output_dir=None):
    if output_dir is None:
        output_dir = BASE_DIR

    output_dir = Path(output_dir)
    return len(list(output_dir.glob(f"{nombre_base}*.pdf"))) > 0


# ============================================================
# SELENIUM SOLO PARA CARGAR CONSULTA INICIAL
# ============================================================

def iniciar_driver():
    options = Options()

    if HEADLESS:
        options.add_argument("--headless=new")

    options.add_argument("--window-size=1600,1200")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-extensions")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    driver.set_page_load_timeout(PAGE_TIMEOUT)
    driver.set_script_timeout(PAGE_TIMEOUT)

    return driver


def aceptar_alerta_si_existe(driver):
    try:
        alert = driver.switch_to.alert
        txt = alert.text
        alert.accept()
        print(f"    Alerta aceptada: {txt[:200]}")
        return txt
    except NoAlertPresentException:
        return None
    except Exception:
        return None


def esperar_ready(driver, timeout=60):
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )


def esperar_sin_loading(driver, timeout=60):
    wait = WebDriverWait(driver, timeout)

    def no_loading(d):
        try:
            loadings = d.find_elements(By.CSS_SELECTOR, "div.loading")
            for l in loadings:
                if l.is_displayed():
                    return False
            return True
        except Exception:
            return True

    try:
        wait.until(no_loading)
    except Exception:
        pass


def obtener_opciones_validas(driver, select_id):
    select = Select(driver.find_element(By.ID, select_id))

    opciones = []

    for opt in select.options:
        value = opt.get_attribute("value")
        text = opt.text.strip()

        if value and value not in ["", "-1"] and "Seleccionar" not in text:
            opciones.append({
                "value": str(value),
                "text": text
            })

    return opciones


def seleccionar_region(driver, region_value, timeout=60):
    wait = WebDriverWait(driver, timeout)

    wait.until(EC.presence_of_element_located((By.ID, ID_REGION)))

    try:
        old_comuna = driver.find_element(By.ID, ID_COMUNA)
    except Exception:
        old_comuna = None

    Select(driver.find_element(By.ID, ID_REGION)).select_by_value(str(region_value))

    if old_comuna is not None:
        try:
            wait.until(EC.staleness_of(old_comuna))
        except Exception:
            pass

    wait.until(lambda d: len(obtener_opciones_validas(d, ID_COMUNA)) > 0)
    esperar_sin_loading(driver)


def seleccionar_comuna(driver, comuna_value, timeout=60):
    wait = WebDriverWait(driver, timeout)
    wait.until(EC.presence_of_element_located((By.ID, ID_COMUNA)))
    Select(driver.find_element(By.ID, ID_COMUNA)).select_by_value(str(comuna_value))
    esperar_sin_loading(driver)


def consultar(driver, timeout=120):
    wait = WebDriverWait(driver, timeout)

    body_antes = driver.find_element(By.TAG_NAME, "body").text

    boton = wait.until(EC.element_to_be_clickable((By.ID, ID_BOTON)))
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", boton)
    time.sleep(0.5)

    esperar_sin_loading(driver)
    driver.execute_script("arguments[0].click();", boton)

    def resultado_actualizado(d):
        try:
            aceptar_alerta_si_existe(d)
            texto = d.find_element(By.TAG_NAME, "body").text

            if texto == body_antes:
                return False

            return (
                "Listado de Viviendas" in texto
                or "Viviendas Precalificadas" in texto
                or "Viviendas Calificadas" in texto
                or "Se ha(n) encontrado" in texto
                or "Identificación Vivienda" in texto
            )
        except Exception:
            return False

    wait.until(resultado_actualizado)
    time.sleep(2.0)
    aceptar_alerta_si_existe(driver)
    esperar_sin_loading(driver)


def cargar_comunas_desde_web(driver):
    driver.get(URL)
    esperar_ready(driver)
    time.sleep(2.5)

    regiones = obtener_opciones_validas(driver, ID_REGION)
    comunas_total = []

    for region in regiones:
        region_value = region["value"]
        region_name = region["text"]

        print(f"Cargando comunas de {region_name}...")

        try:
            seleccionar_region(driver, region_value)
            time.sleep(1.0)

            comunas = obtener_opciones_validas(driver, ID_COMUNA)

            for comuna in comunas:
                comunas_total.append({
                    "region_value": region_value,
                    "region": region_name,
                    "comuna_value": comuna["value"],
                    "comuna": comuna["text"]
                })

        except Exception as e:
            print(f"  ERROR cargando comunas de {region_name}: {e}")
            aceptar_alerta_si_existe(driver)

            try:
                driver.get(URL)
                esperar_ready(driver)
                time.sleep(2)
            except Exception:
                pass

    df = pd.DataFrame(comunas_total)

    if ONLY_ONE_COMUNA:
        mask = (
            df["region"].astype(str).str.contains(TARGET_REGION_CONTAINS, case=False, na=False)
            & (df["comuna"].astype(str).str.lower() == TARGET_COMUNA.lower())
        )
        df = df[mask].copy()

    if not df.empty:
        df = df.drop_duplicates(
            subset=["region_value", "comuna_value"],
            keep="last"
        )
        df = df.sort_values(["region", "comuna"]).reset_index(drop=True)

    return df


def obtener_html_inicial_y_contexto(region_value, comuna_value):
    """
    Selenium solo se usa aquí: cargar región/comuna/consulta y extraer HTML/cookies.
    """
    driver = iniciar_driver()

    try:
        driver.get(URL)
        esperar_ready(driver)
        time.sleep(2.5)

        seleccionar_region(driver, region_value)
        time.sleep(1.0)

        seleccionar_comuna(driver, comuna_value)
        time.sleep(0.8)

        consultar(driver)

        html = driver.page_source
        current_url = driver.current_url

        cookies = {}
        for cookie in driver.get_cookies():
            cookies[cookie["name"]] = cookie["value"]

        try:
            user_agent = driver.execute_script("return navigator.userAgent;")
        except Exception:
            user_agent = "Mozilla/5.0"

        return {
            "html": html,
            "current_url": current_url,
            "cookies": cookies,
            "user_agent": user_agent
        }

    finally:
        try:
            driver.quit()
        except Exception:
            pass


# ============================================================
# PARSEO HTML ASP.NET
# ============================================================

def table_id_por_seccion(seccion):
    if seccion == "precalificadas":
        return "ContentPlaceHolder1_grdViviendasPre"
    elif seccion == "calificadas":
        return "ContentPlaceHolder1_grdViviendasCal"
    else:
        raise ValueError("Sección no válida")


def event_target_por_seccion(seccion):
    if seccion == "precalificadas":
        return "ctl00$ContentPlaceHolder1$grdViviendasPre"
    elif seccion == "calificadas":
        return "ctl00$ContentPlaceHolder1$grdViviendasCal"
    else:
        raise ValueError("Sección no válida")


def celda_visible(tag):
    clases = tag.get("class") or []
    clases = [str(c).lower() for c in clases]
    style = (tag.get("style") or "").lower()

    if "hidde" in clases or "hidden" in clases:
        return False

    if "display:none" in style.replace(" ", ""):
        return False

    return True


def extraer_form_payload(html):
    soup = BeautifulSoup(html, "html.parser")

    form = soup.find("form")
    if form is None:
        raise RuntimeError("No se encontró formulario ASP.NET.")

    payload = {}

    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue

        input_type = (inp.get("type") or "").lower()

        if input_type in ["submit", "button", "image", "file", "reset"]:
            continue

        if input_type in ["checkbox", "radio"]:
            if inp.has_attr("checked"):
                payload[name] = inp.get("value", "on")
            continue

        payload[name] = inp.get("value", "")

    for sel in form.find_all("select"):
        name = sel.get("name")
        if not name:
            continue

        selected = sel.find("option", selected=True)
        if selected is None:
            selected = sel.find("option")

        payload[name] = selected.get("value", "") if selected else ""

    for ta in form.find_all("textarea"):
        name = ta.get("name")
        if name:
            payload[name] = ta.text or ""

    return payload


def obtener_post_url(html, current_url):
    soup = BeautifulSoup(html, "html.parser")
    form = soup.find("form")

    if form is None:
        raise RuntimeError("No se encontró formulario ASP.NET.")

    action = form.get("action") or current_url
    return urljoin(current_url, action)


def extraer_items_pagina_html(html, seccion):
    soup = BeautifulSoup(html, "html.parser")

    table = soup.find("table", id=table_id_por_seccion(seccion))

    if table is None:
        raise RuntimeError(f"No se encontró tabla para sección {seccion}")

    rows = table.find_all("tr")
    items = []

    for row in rows:
        cells_all = row.find_all("td", recursive=False)

        if not cells_all:
            continue

        cells = [td for td in cells_all if celda_visible(td)]

        if len(cells) < 5:
            continue

        text_row = " ".join(td.get_text(" ", strip=True) for td in cells)

        text_simple = text_row.replace("...", "").replace(".", "").strip()
        if text_simple and re.fullmatch(r"[\d\s]+", text_simple):
            continue

        identificacion = cells[0].get_text(" ", strip=True)

        if not identificacion or "Identificación Vivienda" in identificacion:
            continue

        tipologia = cells[1].get_text(" ", strip=True) if len(cells) > 1 else ""
        comuna = cells[2].get_text(" ", strip=True) if len(cells) > 2 else ""
        proyecto = cells[3].get_text(" ", strip=True) if len(cells) > 3 else ""

        accion_cell = cells[-1]

        acciones = []
        for inp in accion_cell.find_all("input"):
            input_type = (inp.get("type") or "").lower()
            title = inp.get("title") or inp.get("alt") or ""
            title_lower = title.lower()

            if input_type != "image":
                continue

            if SOLO_INFORMES:
                if "informe" not in title_lower:
                    continue
            else:
                if "informe" not in title_lower and "etiqueta" not in title_lower:
                    continue

            name = inp.get("name")

            if name:
                acciones.append({
                    "name": name,
                    "title": title or "accion"
                })

        items.append({
            "info": {
                "identificacion": identificacion,
                "tipologia": tipologia,
                "comuna": comuna,
                "proyecto": proyecto
            },
            "acciones": acciones
        })

    return items


def extraer_total_viviendas(html, seccion):
    soup = BeautifulSoup(html, "html.parser")
    texto = soup.get_text(" ", strip=True)

    if seccion == "precalificadas":
        etiqueta = "Viviendas Precalificadas"
        indice_fallback = 0
    elif seccion == "calificadas":
        etiqueta = "Viviendas Calificadas"
        indice_fallback = 1
    else:
        raise ValueError("Sección no válida")

    patron = (
        rf"{etiqueta}.*?"
        r"Se\s+ha\s*\(?n\)?\s+encontrado\s+([\d\.]+)\s+Vivienda"
    )

    m = re.search(patron, texto, flags=re.IGNORECASE)

    if m:
        return int(m.group(1).replace(".", ""))

    nums = re.findall(
        r"Se\s+ha\s*\(?n\)?\s+encontrado\s+([\d\.]+)\s+Vivienda",
        texto,
        flags=re.IGNORECASE
    )

    if len(nums) > indice_fallback:
        return int(nums[indice_fallback].replace(".", ""))

    return 0


def calcular_total_paginas(html, seccion):
    total = extraer_total_viviendas(html, seccion)

    if total <= 0:
        return 0, 0

    total_paginas = math.ceil(total / FILAS_POR_PAGINA)

    return total, total_paginas


# ============================================================
# REQUESTS ASP.NET
# ============================================================

def crear_session(cookies):
    session = requests.Session()

    for k, v in cookies.items():
        session.cookies.set(k, v)

    return session


def post_aspnet(html, current_url, cookies, user_agent, payload_extra):
    post_url = obtener_post_url(html, current_url)
    payload = extraer_form_payload(html)

    payload.update(payload_extra)

    headers = {
        "User-Agent": user_agent,
        "Referer": current_url,
        "Origin": "https://calificacionenergeticaweb.minvu.cl",
        "Connection": "close",
        "Accept": "text/html,application/xhtml+xml,application/xml,application/pdf,*/*",
    }

    session = crear_session(cookies)

    response = session.post(
        post_url,
        data=payload,
        headers=headers,
        timeout=REQUEST_TIMEOUT
    )

    return response


def ir_a_pagina_requests(html, current_url, cookies, user_agent, seccion, pagina_destino):
    event_target = event_target_por_seccion(seccion)

    response = post_aspnet(
        html=html,
        current_url=current_url,
        cookies=cookies,
        user_agent=user_agent,
        payload_extra={
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": f"Page${pagina_destino}"
        }
    )

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code} navegando a página {pagina_destino}")

    content_type = response.headers.get("Content-Type", "")

    if "html" not in content_type.lower() and not response.text.strip().startswith("<"):
        raise RuntimeError(
            f"Respuesta inesperada navegando a página {pagina_destino}. "
            f"Content-Type: {content_type}"
        )

    return response.text


def descargar_accion_requests(
    html,
    current_url,
    cookies,
    user_agent,
    input_name,
    nombre_base,
    output_dir=None
):
    payload_extra = {
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        f"{input_name}.x": "10",
        f"{input_name}.y": "10",
    }

    response = post_aspnet(
        html=html,
        current_url=current_url,
        cookies=cookies,
        user_agent=user_agent,
        payload_extra=payload_extra
    )

    content_type = response.headers.get("Content-Type", "")
    content = response.content

    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}. Content-Type: {content_type}")

    if content.lstrip().startswith(b"%PDF") or "pdf" in content_type.lower():
        return guardar_bytes_descargados(
            content,
            nombre_base,
            content_type,
            output_dir=output_dir
        )

    debug_path = guardar_debug_respuesta(
        nombre_base,
        response,
        output_dir=output_dir
    )

    raise RuntimeError(
        f"La respuesta no parece PDF. Content-Type: {content_type}. Debug: {debug_path}"
    )


def descargar_accion_con_reintentos(
    html,
    current_url,
    cookies,
    user_agent,
    input_name,
    nombre_base,
    output_dir=None
):
    last_error = None

    for intento in range(1, MAX_RETRIES_DOWNLOAD + 1):
        try:
            print(f"        Intento descarga {intento}/{MAX_RETRIES_DOWNLOAD}")

            return descargar_accion_requests(
                html=html,
                current_url=current_url,
                cookies=cookies,
                user_agent=user_agent,
                input_name=input_name,
                nombre_base=nombre_base,
                output_dir=output_dir
            )

        except requests.exceptions.ReadTimeout as e:
            last_error = e
            print("        Timeout descarga. Reintentando...")

        except requests.exceptions.ConnectionError as e:
            last_error = e
            print("        Error conexión descarga. Reintentando...")

        except Exception as e:
            last_error = e
            print(f"        Error descarga intento {intento}: {e}")

        time.sleep(SLEEP_RETRY_BASE * intento)

    raise RuntimeError(f"Descarga falló tras {MAX_RETRIES_DOWNLOAD} intentos: {last_error}")


def ir_a_pagina_con_reintentos(html, current_url, cookies, user_agent, seccion, pagina_destino):
    last_error = None

    for intento in range(1, MAX_RETRIES_PAGE + 1):
        try:
            print(
                f"    Navegando por POST a {seccion}, "
                f"página {pagina_destino}. Intento {intento}/{MAX_RETRIES_PAGE}"
            )

            return ir_a_pagina_requests(
                html=html,
                current_url=current_url,
                cookies=cookies,
                user_agent=user_agent,
                seccion=seccion,
                pagina_destino=pagina_destino
            )

        except requests.exceptions.ReadTimeout as e:
            last_error = e
            print("    Timeout paginación. Reintentando...")

        except requests.exceptions.ConnectionError as e:
            last_error = e
            print("    Error conexión paginación. Reintentando...")

        except Exception as e:
            last_error = e
            print(f"    Error paginación intento {intento}: {e}")

        time.sleep(SLEEP_RETRY_BASE * intento)

    raise RuntimeError(f"No se pudo navegar a página {pagina_destino}: {last_error}")


# ============================================================
# PROCESAMIENTO
# ============================================================

def construir_nombre_base(comuna, seccion, info, accion_title):
    identificacion = info.get("identificacion", "")
    proyecto = info.get("proyecto", "")
    tipologia = info.get("tipologia", "")

    raw = (
        f"ficha_{comuna}_{proyecto}_{identificacion}_"
        f"{tipologia}_{seccion}_{accion_title}"
    )

    return limpiar_nombre_archivo(raw)


def procesar_pagina_html(
    html,
    current_url,
    cookies,
    user_agent,
    region_name,
    comuna_name,
    seccion,
    pagina,
    claves_ok,
    output_dir
):
    items = extraer_items_pagina_html(html, seccion)

    print(f"    {seccion} | página {pagina} | filas visibles: {len(items)}")

    for item in items:
        info = item["info"]
        acciones = item["acciones"]

        identificacion = info["identificacion"]
        proyecto = info["proyecto"]

        if not acciones:
            print(f"      Sin acciones visibles: {identificacion}")
            continue

        for accion in acciones:
            input_name = accion["name"]
            input_title = accion["title"]
            input_title_clean = limpiar_nombre_archivo(input_title, max_len=60)

            nombre_base = construir_nombre_base(
                comuna_name,
                seccion,
                info,
                input_title_clean
            )

            if ya_descargado(
                claves_ok,
                region_name,
                comuna_name,
                seccion,
                identificacion,
                input_title,
                accion_name=input_name
            ):
                print(f"      Saltando ya descargado: {identificacion} | {input_title}")
                continue

            if pdf_ya_existe(nombre_base, output_dir=output_dir):
                print(f"      PDF existente, registrando OK: {identificacion} | {input_title}")

                archivo_existente = output_dir / f"{nombre_base}.pdf"

                claves_ok.add((
                    str(region_name),
                    str(comuna_name),
                    str(seccion),
                    str(identificacion),
                    str(input_title),
                ))

                claves_ok.add((
                    str(region_name),
                    str(comuna_name),
                    str(seccion),
                    str(identificacion),
                    str(input_name),
                ))

                guardar_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "region": region_name,
                    "comuna": comuna_name,
                    "seccion": seccion,
                    "pagina": pagina,
                    "identificacion": identificacion,
                    "proyecto": proyecto,
                    "accion_name": input_name,
                    "accion_title": input_title,
                    "archivo": str(archivo_existente),
                    "estado": "ok",
                    "error": "archivo_existente"
                })

                continue

            print(f"      Descargando: {identificacion} | {proyecto} | {input_title}")

            try:
                archivo = descargar_accion_con_reintentos(
                    html=html,
                    current_url=current_url,
                    cookies=cookies,
                    user_agent=user_agent,
                    input_name=input_name,
                    nombre_base=nombre_base,
                    output_dir=output_dir
                )

                claves_ok.add((
                    str(region_name),
                    str(comuna_name),
                    str(seccion),
                    str(identificacion),
                    str(input_title),
                ))

                claves_ok.add((
                    str(region_name),
                    str(comuna_name),
                    str(seccion),
                    str(identificacion),
                    str(input_name),
                ))

                guardar_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "region": region_name,
                    "comuna": comuna_name,
                    "seccion": seccion,
                    "pagina": pagina,
                    "identificacion": identificacion,
                    "proyecto": proyecto,
                    "accion_name": input_name,
                    "accion_title": input_title,
                    "archivo": str(archivo),
                    "estado": "ok",
                    "error": ""
                })

                print(f"        OK: {archivo}")

            except Exception as e:
                guardar_log({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "region": region_name,
                    "comuna": comuna_name,
                    "seccion": seccion,
                    "pagina": pagina,
                    "identificacion": identificacion,
                    "proyecto": proyecto,
                    "accion_name": input_name,
                    "accion_title": input_title,
                    "archivo": "",
                    "estado": "error_vivienda",
                    "error": str(e)
                })

                print(f"        ERROR vivienda omitida: {e}")

            time.sleep(PAUSA_ENTRE_DESCARGAS)


def procesar_seccion_requests(
    html_inicial,
    current_url,
    cookies,
    user_agent,
    region_name,
    comuna_name,
    seccion,
    claves_ok,
    output_dir
):
    total, total_paginas = calcular_total_paginas(html_inicial, seccion)

    print(f"    Total {seccion}: {total} viviendas")
    print(f"    Total páginas {seccion}: {total_paginas}")

    if total_paginas == 0:
        print(f"    No hay viviendas en sección {seccion}")
        return

    for pagina in range(1, total_paginas + 1):
        try:
            if pagina == 1:
                html_actual = html_inicial
            else:
                html_actual = ir_a_pagina_con_reintentos(
                    html=html_inicial,
                    current_url=current_url,
                    cookies=cookies,
                    user_agent=user_agent,
                    seccion=seccion,
                    pagina_destino=pagina
                )

            procesar_pagina_html(
                html=html_actual,
                current_url=current_url,
                cookies=cookies,
                user_agent=user_agent,
                region_name=region_name,
                comuna_name=comuna_name,
                seccion=seccion,
                pagina=pagina,
                claves_ok=claves_ok,
                output_dir=output_dir
            )

        except Exception as e:
            guardar_log({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "region": region_name,
                "comuna": comuna_name,
                "seccion": seccion,
                "pagina": pagina,
                "identificacion": "",
                "proyecto": "",
                "accion_name": "",
                "accion_title": "",
                "archivo": "",
                "estado": "error_pagina_omitida",
                "error": str(e)
            })

            print(f"    ERROR página {pagina} de {seccion}. Se omite y se sigue: {e}")

        time.sleep(PAUSA_ENTRE_PAGINAS)

    print(f"    Fin sección {seccion}. Páginas procesadas: {total_paginas}")


# ============================================================
# MAIN
# ============================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="Descarga fichas PDF CEV desde el buscador publico del MINVU."
    )
    parser.add_argument(
        "--paths-config",
        type=Path,
        default=default_paths_config(Path(__file__).resolve()),
        help="Archivo YAML con rutas del proyecto.",
    )
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--log-csv", type=Path, default=None)
    parser.add_argument("--only-one-comuna", action="store_true")
    parser.add_argument("--target-region-contains", type=str, default=None)
    parser.add_argument("--target-comuna", type=str, default=None)
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None)
    return parser.parse_args()


def configure_from_args(args) -> None:
    global BASE_DIR, LOG_CSV, ONLY_ONE_COMUNA, TARGET_REGION_CONTAINS, TARGET_COMUNA, HEADLESS

    paths = load_paths(args.paths_config)
    BASE_DIR = args.output_dir or path_value(paths, "cev_pdf_dir", BASE_DIR)
    LOG_CSV = args.log_csv or path_value(paths, "download_log_csv_path", LOG_CSV)

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)

    if args.only_one_comuna:
        ONLY_ONE_COMUNA = True
    if args.target_region_contains:
        TARGET_REGION_CONTAINS = args.target_region_contains
    if args.target_comuna:
        TARGET_COMUNA = args.target_comuna
    if args.headless is not None:
        HEADLESS = args.headless


def main():
    args = parse_args()
    configure_from_args(args)

    print(f"Carpeta de trabajo: {BASE_DIR}")
    print(f"Log CSV: {LOG_CSV}")
    print(f"ONLY_ONE_COMUNA = {ONLY_ONE_COMUNA}")

    driver = iniciar_driver()

    try:
        df_comunas = cargar_comunas_desde_web(driver)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    if df_comunas.empty:
        print("No se encontraron comunas para procesar.")
        return

    print(f"Comunas a procesar: {len(df_comunas)}")

    if ONLY_ONE_COMUNA:
        print(f"Filtro: {TARGET_REGION_CONTAINS} / {TARGET_COMUNA}")
        print(df_comunas[["region", "comuna", "region_value", "comuna_value"]])

    claves_ok = cargar_log_ok()

    for _, row in df_comunas.iterrows():
        region_value = str(row["region_value"])
        comuna_value = str(row["comuna_value"])
        region_name = str(row["region"])
        comuna_name = str(row["comuna"])

        output_dir = carpeta_comuna(comuna_name)

        print("\n====================================================")
        print(f"Procesando {region_name} / {comuna_name}")
        print(f"region_value={region_value} | comuna_value={comuna_value}")
        print(f"Carpeta salida: {output_dir}")
        print("====================================================")

        try:
            contexto = obtener_html_inicial_y_contexto(region_value, comuna_value)

            html_inicial = contexto["html"]
            current_url = contexto["current_url"]
            cookies = contexto["cookies"]
            user_agent = contexto["user_agent"]

            for seccion in SECCIONES:
                print(f"\n--- Procesando sección: {seccion} ---")

                procesar_seccion_requests(
                    html_inicial=html_inicial,
                    current_url=current_url,
                    cookies=cookies,
                    user_agent=user_agent,
                    region_name=region_name,
                    comuna_name=comuna_name,
                    seccion=seccion,
                    claves_ok=claves_ok,
                    output_dir=output_dir
                )

            time.sleep(PAUSA_ENTRE_COMUNAS)

        except Exception as e:
            guardar_log({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "region": region_name,
                "comuna": comuna_name,
                "seccion": "",
                "pagina": "",
                "identificacion": "",
                "proyecto": "",
                "accion_name": "",
                "accion_title": "",
                "archivo": "",
                "estado": "error_comuna",
                "error": str(e)
            })

            print(f"ERROR comuna {region_name} / {comuna_name}: {e}")


if __name__ == "__main__":
    main()