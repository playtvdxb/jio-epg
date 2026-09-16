import requests
import json
import os
import sys
import time
import random
import gzip
import shutil
from xml.sax.saxutils import escape as xml_escape

from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed


# ============================================================
# CONFIG
# ============================================================

CHANNELS_FILE = "channels.json"
EPG_XML_FILE = "epg.xml"
EPG_XML_GZ_FILE = "epg.xml.gz"


# ============================================================
# XML HELPERS
# ============================================================

def safe_xml(text):
    if text is None:
        return ""
    return xml_escape(str(text), {'"': "&quot;", "'": "&apos;"})


def format_xmltv_datetime(dt_str):
    if not dt_str:
        return ""
    clean_str = dt_str.replace("-", "").replace(":", "").replace("T", "").strip()
    if len(clean_str) == 14:
        return f"{clean_str} +0530"
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%Y%m%d%H%M%S +0530")
    except Exception:
        return ""


# ============================================================
# JIOTV EPG API
# ============================================================

EPG_API_URL = (
    "https://jiotvapi.cdn.jio.com/apis/v1.3/getepg/get"
    "?channel_id={channel_id}"
    "&offset={offset}"
)


# ============================================================
# HPROXY API
# ============================================================

HPROXY_URL = (
    "https://hproxy.com/api/proxy-list"
    "?format=json"
    "&country=IN"
    "&anonymity=anonymous"
    "&protocol=http,https"
)


# ============================================================
# PROXY TEST
# ============================================================

PROXY_TEST_CHANNEL = 2934
PROXY_TEST_OFFSET = 0


# ============================================================
# BATCH SETTINGS
# ============================================================

BATCH_SIZE = 50


# ============================================================
# RETRY SETTINGS
# ============================================================

MAX_RETRIES = 4
REQUEST_TIMEOUT = 30
PROXY_TEST_TIMEOUT = 10


# ============================================================
# OFFSETS  (0 = today, 1 = tomorrow)
# ============================================================

OFFSETS = [
    0,
    1
]


# ============================================================
# KILL-SWITCH
# ============================================================

# Refuse to commit if fewer than this many programmes were collected.
# Prevents a broken proxy run from overwriting your good feed.
MIN_PROGRAMMES_TO_COMMIT = 30000


# ============================================================
# EPG IMAGE BASE URL
# ============================================================

EPG_IMAGE_URL = "https://jiotvimages.cdn.jio.com/"


# ============================================================
# HEADERS
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 "
        "(Windows NT 10.0; Win64; x64; "
        "rv:153.0) "
        "Gecko/20100101 Firefox/153.0"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.jiotv.com/",
    "Origin": "https://www.jiotv.com",
    "Connection": "keep-alive",
    "Cache-Control": "no-cache"
}


# ============================================================
# GLOBAL WORKING PROXY
# ============================================================

WORKING_PROXY = None


# ============================================================
# LOAD CHANNELS.JSON
# ============================================================

def load_channels():
    if not os.path.exists(CHANNELS_FILE):
        print(f"ERROR: {CHANNELS_FILE} not found.")
        return []

    try:
        with open(CHANNELS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
    except Exception as e:
        print("ERROR reading channels.json:")
        print(e)
        return []

    if not isinstance(data, list):
        print("ERROR: channels.json must contain a JSON array.")
        return []

    return data


# ============================================================
# EXTRACT EPG
# ============================================================

def extract_epg(data):
    if not isinstance(data, dict):
        return []

    if isinstance(data.get("epg"), list):
        return data["epg"]

    if isinstance(data.get("result"), list):
        return data["result"]

    if isinstance(data.get("data"), list):
        return data["data"]

    result = data.get("result")
    if isinstance(result, dict):
        if isinstance(result.get("epg"), list):
            return result["epg"]
        if isinstance(result.get("data"), list):
            return result["data"]

    return []


# ============================================================
# GET PROXIES FROM HPROXY API
# ============================================================

def get_hproxy_proxies():
    print()
    print("=" * 70)
    print("FETCHING INDIA PROXIES FROM HPROXY API")
    print("=" * 70)

    try:
        response = requests.get(HPROXY_URL, headers=HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print("ERROR fetching HProxy API:")
        print(e)
        return []

    if not isinstance(data, list):
        print("ERROR: HProxy API returned unexpected data.")
        return []

    proxies = []
    for item in data:
        if not isinstance(item, dict):
            continue

        ip = item.get("ip")
        port = item.get("port")
        protocols = item.get("protocols", [])
        status = item.get("status")

        if not ip or not port:
            continue

        if status != "alive":
            continue

        protocols = [str(p).lower() for p in protocols]

        if "http" not in protocols and "https" not in protocols:
            continue

        proxy = f"{ip}:{port}"

        proxies.append({
            "proxy": proxy,
            "protocols": protocols,
            "latency_ms": item.get("latency_ms"),
            "uptime_24h": item.get("uptime_24h"),
            "uptime_7d": item.get("uptime_7d"),
            "uptime_pct": item.get("uptime_pct"),
            "reliability": item.get("reliability"),
            "verification_count": item.get("verification_count")
        })

    # Remove duplicates
    unique = {}
    for item in proxies:
        unique[item["proxy"]] = item
    proxies = list(unique.values())

    # Sort by latency
    proxies.sort(
        key=lambda item: (
            item.get("latency_ms")
            if item.get("latency_ms") is not None
            else 999999
        )
    )

    print(f"Found {len(proxies)} alive HTTP/HTTPS proxies.")
    print()

    for item in proxies:
        print(
            f"{item['proxy']} | "
            f"protocols={','.join(item['protocols'])} | "
            f"latency={item['latency_ms']}ms | "
            f"uptime24h={item['uptime_24h']}% | "
            f"reliability={item['reliability']}"
        )

    return proxies


# ============================================================
# TEST ONE PROXY AGAINST JIOTV
# ============================================================

def test_proxy(proxy_info):
    proxy = proxy_info["proxy"]

    test_url = EPG_API_URL.format(
        channel_id=PROXY_TEST_CHANNEL,
        offset=PROXY_TEST_OFFSET
    )

    proxy_url = f"http://{proxy}"
    proxy_config = {"http": proxy_url, "https": proxy_url}

    print(f"Testing proxy: {proxy}")

    try:
        response = requests.get(
            test_url,
            headers=HEADERS,
            proxies=proxy_config,
            timeout=PROXY_TEST_TIMEOUT
        )

        status = response.status_code
        print(f"  {proxy} -> HTTP {status}")

        if status != 200:
            return None

        try:
            data = response.json()
        except Exception:
            print(f"  {proxy} -> invalid JSON")
            return None

        epg = extract_epg(data)
        if not epg:
            print(f"  {proxy} -> 200 but no EPG data")
            return None

        print()
        print(f"  [WORKING JIOTV PROXY] {proxy}")
        return proxy

    except requests.exceptions.ProxyError:
        print(f"  [PROXY ERROR] {proxy}")
    except requests.exceptions.ConnectTimeout:
        print(f"  [CONNECT TIMEOUT] {proxy}")
    except requests.exceptions.ReadTimeout:
        print(f"  [READ TIMEOUT] {proxy}")
    except requests.exceptions.ConnectionError:
        print(f"  [CONNECTION ERROR] {proxy}")
    except Exception as e:
        print(f"  [ERROR] {proxy} - {e}")

    return None


# ============================================================
# FIND WORKING PROXY
# ============================================================

def find_working_proxy():
    global WORKING_PROXY

    proxy_list = get_hproxy_proxies()

    if not proxy_list:
        print("No HProxy candidates found.")
        return None

    print()
    print("=" * 70)
    print("TESTING PROXIES AGAINST JIOTV EPG")
    print("=" * 70)

    max_proxy_workers = min(10, len(proxy_list))

    with ThreadPoolExecutor(max_workers=max_proxy_workers) as executor:
        futures = {
            executor.submit(test_proxy, proxy_info): proxy_info
            for proxy_info in proxy_list
        }

        for future in as_completed(futures):
            try:
                result = future.result()

                if result:
                    WORKING_PROXY = result

                    print()
                    print("=" * 70)
                    print("WORKING PROXY FOUND")
                    print(f"Proxy: {WORKING_PROXY}")
                    print("=" * 70)

                    for pending in futures:
                        if not pending.done():
                            pending.cancel()

                    return WORKING_PROXY

            except Exception as e:
                print(f"Proxy test error: {e}")

    print()
    print("=" * 70)
    print("NO WORKING JIOTV PROXY FOUND")
    print("=" * 70)

    return None


# ============================================================
# CREATE SESSION
# ============================================================

def create_session():
    session = requests.Session()
    session.headers.update(HEADERS)

    if WORKING_PROXY:
        proxy_url = f"http://{WORKING_PROXY}"
        session.proxies.update({
            "http": proxy_url,
            "https": proxy_url
        })

    return session


# ============================================================
# THUMBNAIL URL
# ============================================================

def get_thumbnail_url(path):
    if not path:
        return None

    if path.startswith("http://") or path.startswith("https://"):
        return path

    path = path.lstrip("/")
    return EPG_IMAGE_URL + path


# ============================================================
# SERVER DATE
# ============================================================

def get_server_date(server_date):
    if not server_date:
        return None

    try:
        dt = datetime.fromisoformat(server_date)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return server_date.split("T")[0]


# ============================================================
# CREATE DATETIME
# ============================================================

def create_datetime(server_date, time_string):
    if not server_date or not time_string:
        return None

    try:
        if len(time_string) == 5:
            time_part = datetime.strptime(time_string, "%H:%M").time()
        else:
            time_part = datetime.strptime(time_string, "%H:%M:%S").time()

        date_part = datetime.strptime(server_date, "%Y-%m-%d").date()

        result = datetime.combine(date_part, time_part)
        return result.strftime("%Y-%m-%dT%H:%M:%S")

    except Exception:
        return None


# ============================================================
# PROCESS PROGRAM
# ============================================================

def process_program(program, offset_server_date):
    showtime = program.get("showtime")
    endtime = program.get("endtime")

    server_date = offset_server_date

    start_date = create_datetime(server_date, showtime)
    end_date = create_datetime(server_date, endtime)

    # Handle midnight crossing
    if start_date and end_date and end_date < start_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%dT%H:%M:%S")
            end_dt += timedelta(days=1)
            end_date = end_dt.strftime("%Y-%m-%dT%H:%M:%S")
        except Exception:
            pass

    thumbnail = (
        program.get("episodeThumbnail")
        or program.get("episodePoster")
        or program.get("thumbnail")
        or program.get("thumbnailUrl")
    )

    thumbnail_url = get_thumbnail_url(thumbnail)

    return {
        "serverDate": server_date,
        "showName": program.get("showname"),
        "description": program.get("description"),
        "startDate": start_date,
        "endDate": end_date,
        "showTime": showtime,
        "endTime": endtime,
        "showCategory": program.get("showCategory"),
        "thumbnailUrl": thumbnail_url
    }


# ============================================================
# GET EPG WITH RETRIES
# ============================================================

def get_epg(session, channel_id, offset):
    url = EPG_API_URL.format(channel_id=channel_id, offset=offset)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            status = response.status_code

            # HTTP 450 — JioTV "too many requests" style block
            if status == 450:
                wait_time = 3 * (2 ** (attempt - 1)) + random.uniform(0.5, 2.5)

                if attempt < MAX_RETRIES:
                    print(
                        f"      Channel {channel_id} Offset {offset}: "
                        f"HTTP 450 - retrying in {wait_time:.1f}s"
                    )
                    time.sleep(wait_time)
                    continue

                print(
                    f"      Channel {channel_id} Offset {offset}: "
                    f"HTTP 450 after {MAX_RETRIES} attempts"
                )
                return []

            # HTTP 429 — rate limited
            if status == 429:
                retry_after = response.headers.get("Retry-After")

                if retry_after:
                    try:
                        wait_time = float(retry_after)
                    except ValueError:
                        wait_time = 10
                else:
                    wait_time = 5 * (2 ** (attempt - 1)) + random.uniform(1, 3)

                if attempt < MAX_RETRIES:
                    print(
                        f"      Channel {channel_id} Offset {offset}: "
                        f"HTTP 429 - waiting {wait_time:.1f}s"
                    )
                    time.sleep(wait_time)
                    continue

                print(
                    f"      Channel {channel_id} Offset {offset}: "
                    f"HTTP 429 after {MAX_RETRIES} attempts"
                )
                return []

            response.raise_for_status()
            data = response.json()
            return extract_epg(data)

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < MAX_RETRIES:
                wait_time = 2 ** attempt + random.uniform(0.5, 2)
                print(
                    f"      Channel {channel_id} Offset {offset}: "
                    f"connection error - retrying in {wait_time:.1f}s"
                )
                time.sleep(wait_time)
                continue

            print(
                f"      Channel {channel_id} Offset {offset} ERROR: {e}"
            )
            return []

        except requests.exceptions.JSONDecodeError:
            print(
                f"      Channel {channel_id} Offset {offset}: Invalid JSON"
            )
            return []

        except requests.exceptions.RequestException as e:
            print(
                f"      Channel {channel_id} Offset {offset} ERROR: {e}"
            )
            return []

        except Exception as e:
            print(
                f"      Channel {channel_id} Offset {offset} ERROR: {e}"
            )
            return []

    return []


# ============================================================
# PROCESS ONE CHANNEL
# ============================================================

def process_channel(channel):
    channel_id = channel.get("channel_id")
    channel_name = channel.get("channel_name")
    logo_url = channel.get("logoUrl")
    language_id = channel.get("language_id")
    language = channel.get("language")
    category_id = channel.get("category_id")
    category = channel.get("category")

    if channel_id is None:
        return (False, None, "Missing channel_id", None)

    session = create_session()
    all_programs = []

    for offset in OFFSETS:
        epg_data = get_epg(session, channel_id, offset)

        if not epg_data:
            continue

        offset_server_date = None
        for program in epg_data:
            raw_server_date = program.get("serverDate")
            if raw_server_date:
                offset_server_date = get_server_date(raw_server_date)
                break

        if not offset_server_date:
            continue

        print(
            f"Channel {channel_id} | "
            f"Offset {offset} | "
            f"Date {offset_server_date} | "
            f"Programs {len(epg_data)}"
        )

        for program in epg_data:
            program_channel_id = program.get("channel_id")

            if (
                program_channel_id is not None
                and str(program_channel_id) != str(channel_id)
            ):
                continue

            processed = process_program(program, offset_server_date)
            all_programs.append(processed)

    # Remove duplicates
    unique_programs = {}
    for program in all_programs:
        key = (
            program.get("serverDate"),
            program.get("showTime"),
            program.get("endTime"),
            program.get("showName")
        )
        unique_programs[key] = program

    all_programs = list(unique_programs.values())

    # Sort
    all_programs.sort(key=lambda item: item.get("startDate") or "")

    channel_output = {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "language_id": language_id,
        "language": language,
        "category_id": category_id,
        "category": category,
        "logoUrl": logo_url,
        "programs": all_programs
    }

    return (True, channel_id, len(all_programs), channel_output)


# ============================================================
# PROCESS ONE BATCH OF CHANNELS
# ============================================================

def process_batch(batch, batch_number, total_batches):
    print()
    print("=" * 70)
    print(f"BATCH {batch_number}/{total_batches}")
    print(f"Channels: {len(batch)}")
    print(f"Proxy: {WORKING_PROXY}")
    print("=" * 70)

    completed = 0
    failed = 0
    batch_channels = []

    with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
        futures = {
            executor.submit(process_channel, channel): channel
            for channel in batch
        }

        for future in as_completed(futures):
            channel = futures[future]
            channel_id = channel.get("channel_id")
            channel_name = channel.get("channel_name")

            try:
                success, result_id, result, channel_data = future.result()

                if success:
                    completed += 1
                    if channel_data:
                        batch_channels.append(channel_data)

                    print(f"[OK] {result_id} - {channel_name} - {result} programs")
                else:
                    failed += 1
                    print(f"[FAILED] {channel_id} - {channel_name} - {result}")

            except Exception as e:
                failed += 1
                print(f"[FAILED] {channel_id} - {channel_name} - {e}")

    print()
    print(f"Batch {batch_number} completed")
    print(f"Successful: {completed}")
    print(f"Failed: {failed}")

    return batch_channels


# ============================================================
# LOAD ALL CHANNEL DATA
# ============================================================

def load_all_channel_data(channels_meta, in_memory_channels=None):
    channels_dict = {}

    if in_memory_channels:
        for ch in in_memory_channels:
            cid = str(ch.get("channel_id"))
            channels_dict[cid] = ch

    final_channels = []

    for ch_meta in channels_meta:
        cid = str(ch_meta.get("channel_id"))

        if cid in channels_dict:
            final_channels.append(channels_dict[cid])
        else:
            final_channels.append({
                "channel_id": ch_meta.get("channel_id"),
                "channel_name": ch_meta.get("channel_name"),
                "language_id": ch_meta.get("language_id"),
                "language": ch_meta.get("language"),
                "category_id": ch_meta.get("category_id"),
                "category": ch_meta.get("category"),
                "logoUrl": ch_meta.get("logoUrl"),
                "programs": []
            })

    return final_channels


# ============================================================
# GENERATE XMLTV GUIDE AND GZIP COMPRESSED XMLTV
# ============================================================

def generate_xmltv(channels_data, xml_file=EPG_XML_FILE, gz_file=EPG_XML_GZ_FILE):
    print()
    print("=" * 70)
    print("GENERATING XMLTV EPG (XML & XML.GZ)")
    print("=" * 70)

    total_programs = 0

    try:
        with open(xml_file, "w", encoding="utf-8") as file:
            file.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            file.write('<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
            file.write('<tv generator-info-name="jiotv-epg" source-info-name="JioTV">\n')

            # 1. Channel entries
            for ch in channels_data:
                ch_id = safe_xml(ch.get("channel_id"))
                ch_name = safe_xml(ch.get("channel_name"))
                logo = safe_xml(ch.get("logoUrl"))

                file.write(f'  <channel id="{ch_id}">\n')
                file.write(f'    <display-name lang="en">{ch_name}</display-name>\n')
                if logo:
                    file.write(f'    <icon src="{logo}" />\n')
                file.write('  </channel>\n')

            # 2. Programme entries
            for ch in channels_data:
                ch_id = safe_xml(ch.get("channel_id"))
                programs = ch.get("programs", [])

                for prog in programs:
                    start = format_xmltv_datetime(prog.get("startDate"))
                    stop = format_xmltv_datetime(prog.get("endDate"))

                    if not start or not stop:
                        continue

                    title = safe_xml(prog.get("showName") or ch.get("channel_name") or "No Information")
                    desc = safe_xml(prog.get("description"))
                    category = safe_xml(prog.get("showCategory") or ch.get("category"))
                    thumb = safe_xml(prog.get("thumbnailUrl"))

                    file.write(f'  <programme start="{start}" stop="{stop}" channel="{ch_id}">\n')
                    file.write(f'    <title lang="en">{title}</title>\n')

                    if desc:
                        file.write(f'    <desc lang="en">{desc}</desc>\n')

                    if category:
                        file.write(f'    <category lang="en">{category}</category>\n')

                    if thumb:
                        file.write(f'    <icon src="{thumb}" />\n')

                    file.write('  </programme>\n')
                    total_programs += 1

            file.write('</tv>\n')

        # 3. Gzip compression
        with open(xml_file, "rb") as f_in:
            with gzip.open(gz_file, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)

        xml_size_mb = os.path.getsize(xml_file) / (1024 * 1024)
        gz_size_mb = os.path.getsize(gz_file) / (1024 * 1024)

        print(f"Channels written: {len(channels_data)}")
        print(f"Programmes written: {total_programs}")
        print(f"Generated XML: {xml_file} ({xml_size_mb:.2f} MB)")
        print(f"Generated GZ:  {gz_file} ({gz_size_mb:.2f} MB)")

        return total_programs

    except Exception as e:
        print(f"ERROR generating XMLTV EPG: {e}")
        return 0


# ============================================================
# MAIN
# ============================================================

def main():
    print()
    print("=" * 70)
    print("JIO TV EPG CHANNEL GENERATOR")
    print("=" * 70)

    # ========================================================
    # STEP 1 — FIND WORKING INDIA PROXY
    # ========================================================
    working_proxy = find_working_proxy()

    if not working_proxy:
        print()
        print("STOPPING.")
        print("No working India proxy could access JioTV EPG.")
        sys.exit(1)

    # ========================================================
    # STEP 2 — LOAD CHANNELS
    # ========================================================
    channels = load_channels()

    if not channels:
        print("No channels found.")
        sys.exit(1)

    print()
    print(f"Total channels: {len(channels)}")
    print(f"Channels per batch: {BATCH_SIZE}")
    print(f"Offsets: {OFFSETS}")
    print(f"Working proxy: {working_proxy}")

    # ========================================================
    # STEP 3 — SPLIT INTO BATCHES
    # ========================================================
    batches = [
        channels[i:i + BATCH_SIZE]
        for i in range(0, len(channels), BATCH_SIZE)
    ]
    total_batches = len(batches)

    print()
    print(f"Total batches: {total_batches}")

    # ========================================================
    # STEP 4 — PROCESS BATCHES
    # ========================================================
    all_collected_channels = []

    for index, batch in enumerate(batches, start=1):
        batch_channels = process_batch(batch, index, total_batches)
        all_collected_channels.extend(batch_channels)

    # ========================================================
    # STEP 5 — GENERATE XMLTV EPG
    # ========================================================
    all_channels_data = load_all_channel_data(channels, all_collected_channels)

    total_programs = sum(len(c.get("programs", [])) for c in all_channels_data)
    channels_with_data = sum(1 for c in all_channels_data if c.get("programs"))

    print()
    print("=" * 70)
    print(f"Channels with data: {channels_with_data}")
    print(f"Total programmes:   {total_programs}")
    print("=" * 70)

    # Kill-switch — refuse to commit a broken feed
    if total_programs < MIN_PROGRAMMES_TO_COMMIT:
        print()
        print("=" * 70)
        print(f"ONLY {total_programs} PROGRAMMES — ABORTING")
        print(f"Minimum required: {MIN_PROGRAMMES_TO_COMMIT}")
        print("Refusing to overwrite epg.xml.gz with incomplete feed.")
        print("GitHub Actions will mark this run as failed.")
        print("=" * 70)
        sys.exit(1)

    generate_xmltv(all_channels_data, EPG_XML_FILE, EPG_XML_GZ_FILE)

    # ========================================================
    # DONE
    # ========================================================
    print()
    print("=" * 70)
    print("ALL CHANNELS COMPLETED")
    print(f"Proxy used: {WORKING_PROXY}")
    print(f"Output XML: {EPG_XML_FILE}")
    print(f"Output GZ:  {EPG_XML_GZ_FILE}")
    print("=" * 70)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
