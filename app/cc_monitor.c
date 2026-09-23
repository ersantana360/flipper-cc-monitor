// Claude Code Monitor: poll the bridge over the WiFi dev board (FlipperHTTP),
// draw the session state, and send allow/deny on OK/BACK when a decision is pending.
#include <furi.h>
#include <gui/gui.h>
#include <input/input.h>
#include <storage/storage.h>
#include <notification/notification_messages.h>
#include <expansion/expansion.h>
#include "flipper_http/flipper_http.h"

#define TAG "CCMonitor"
#define CONFIG_DIR EXT_PATH("apps_data/cc_monitor")
#define CONFIG_PATH CONFIG_DIR "/bridge.txt"
#define WIFI_PATH CONFIG_DIR "/wifi.txt"   // optional: line 1 = SSID, line 2 = password
#define DEFAULT_BRIDGE "http://192.168.0.9:8730"
#define POLL_MS 1000
#define REQUEST_TIMEOUT_MS 4000

typedef enum {
    ConnBoot,     // starting up
    ConnNoBoard,  // no PONG from the dev board
    ConnOffline,  // board answers but the bridge is unreachable
    ConnOnline,   // last poll succeeded
} ConnState;

typedef enum {
    EvtStop = (1 << 0),
    EvtDecision = (1 << 1),
} WorkerEvt;

typedef struct {
    FuriMutex* mutex;
    FlipperHTTP* fhttp;
    FuriThread* worker;
    ViewPort* vp;
    NotificationApp* notif;

    char bridge[96];
    char status[24];
    char detail[80];
    char ask_id[12];
    char last_line[48];   // last interesting line from the board, for the status row
    bool pending;
    bool pong;
    ConnState conn;
    uint32_t fails;
    char decision[8];     // "allow"/"deny" queued for the worker
    bool running;
} App;

// ---------- tiny helpers ----------

// Copy the string value of "key" from a flat JSON object (no escapes; the bridge sanitizes).
static bool json_str(const char* json, const char* key, char* out, size_t n) {
    char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\":\"", key);
    const char* p = strstr(json, pat);
    if(!p) return false;
    p += strlen(pat);
    size_t i = 0;
    while(*p && *p != '"' && i + 1 < n) out[i++] = *p++;
    out[i] = 0;
    return true;
}

static void load_bridge_url(App* app) {
    strncpy(app->bridge, DEFAULT_BRIDGE, sizeof(app->bridge) - 1);
    Storage* storage = furi_record_open(RECORD_STORAGE);
    storage_simply_mkdir(storage, CONFIG_DIR);
    File* f = storage_file_alloc(storage);
    if(storage_file_open(f, CONFIG_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
        char buf[96] = {0};
        size_t n = storage_file_read(f, buf, sizeof(buf) - 1);
        buf[n] = 0;
        // first line, trimmed
        char* e = buf;
        while(*e && *e != '\r' && *e != '\n') e++;
        *e = 0;
        char* s = buf;
        while(*s == ' ') s++;
        if(strlen(s) > 7) strncpy(app->bridge, s, sizeof(app->bridge) - 1);
    } else {
        storage_file_close(f);
        if(storage_file_open(f, CONFIG_PATH, FSAM_WRITE, FSOM_CREATE_ALWAYS)) {
            storage_file_write(f, DEFAULT_BRIDGE, strlen(DEFAULT_BRIDGE));
            storage_file_write(f, "\n", 1);
        }
    }
    storage_file_close(f);
    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);
    FURI_LOG_I(TAG, "bridge: %s", app->bridge);
}

// Optional WiFi credentials file: pushed to the board with [WIFI/SAVE] on every start.
static bool load_wifi(char* ssid, size_t ns, char* pass, size_t np) {
    bool ok = false;
    Storage* storage = furi_record_open(RECORD_STORAGE);
    File* f = storage_file_alloc(storage);
    if(storage_file_open(f, WIFI_PATH, FSAM_READ, FSOM_OPEN_EXISTING)) {
        char buf[160] = {0};
        size_t n = storage_file_read(f, buf, sizeof(buf) - 1);
        buf[n] = 0;
        char* line2 = buf;
        while(*line2 && *line2 != '\r' && *line2 != '\n') line2++;
        if(*line2) {
            *line2++ = 0;
            while(*line2 == '\r' || *line2 == '\n') line2++;
            char* e = line2;
            while(*e && *e != '\r' && *e != '\n') e++;
            *e = 0;
            if(buf[0] && line2[0]) {
                strncpy(ssid, buf, ns - 1);
                strncpy(pass, line2, np - 1);
                ok = true;
            }
        }
    }
    storage_file_close(f);
    storage_file_free(f);
    furi_record_close(RECORD_STORAGE);
    return ok;
}

// Called by FlipperHTTP for every line the board sends.
static void rx_line(const char* line, void* ctx) {
    App* app = ctx;
    if(strstr(line, "[PONG]")) app->pong = true;
    if(strstr(line, "[ERROR]") || strstr(line, "[INFO]") || strstr(line, "[CONNECTED]") ||
       strstr(line, "[DISCONNECTED]")) {
        furi_mutex_acquire(app->mutex, FuriWaitForever);
        strncpy(app->last_line, line, sizeof(app->last_line) - 1);
        furi_mutex_release(app->mutex);
    }
}

// Send a request and wait for the [.../END] marker. Returns true on HTTP 200.
static bool http_do(App* app, HTTPMethod method, const char* url, const char* payload) {
    FlipperHTTP* f = app->fhttp;
    f->state = IDLE;
    memset(f->last_response, 0, RX_BUF_SIZE);
    f->status_code = 0;
    bool sent = (method == GET) ? flipper_http_request(f, GET, url, NULL, NULL) :
                                  flipper_http_request(f, POST, url, "{\"Content-Type\":\"text/plain\"}", payload);
    if(!sent) return false;
    f->state = RECEIVING;
    uint32_t t0 = furi_get_tick();
    while(f->state == RECEIVING && furi_get_tick() - t0 < REQUEST_TIMEOUT_MS) furi_delay_ms(50);
    bool ok = (f->state == IDLE) && f->status_code == 200;
    if(f->state != IDLE) f->state = IDLE;   // recover from ISSUE / timeout
    return ok;
}

static bool board_ping(App* app) {
    app->pong = false;
    app->fhttp->state = IDLE;
    if(!flipper_http_send_command(app->fhttp, HTTP_CMD_PING)) return false;
    for(int i = 0; i < 30 && !app->pong; i++) furi_delay_ms(100);
    app->fhttp->state = IDLE;
    return app->pong;
}

static void poll_state(App* app) {
    char url[160];
    snprintf(url, sizeof(url), "%s/state?client=flipper", app->bridge);
    bool ok = http_do(app, GET, url, NULL);
    char status[24] = {0}, detail[80] = {0}, pend[4] = {0}, ask[12] = {0};
    if(ok) {
        const char* r = app->fhttp->last_response;
        ok = json_str(r, "status", status, sizeof(status));
        json_str(r, "detail", detail, sizeof(detail));
        json_str(r, "pending", pend, sizeof(pend));
        json_str(r, "ask", ask, sizeof(ask));
    }
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    if(ok) {
        bool was_pending = app->pending;
        bool new_ask = strcmp(ask, app->ask_id) != 0;
        strncpy(app->status, status, sizeof(app->status) - 1);
        strncpy(app->detail, detail, sizeof(app->detail) - 1);
        app->pending = (pend[0] == '1');
        strncpy(app->ask_id, ask, sizeof(app->ask_id) - 1);
        app->conn = ConnOnline;
        app->fails = 0;
        if(app->pending && (!was_pending || new_ask)) {
            notification_message(app->notif, &sequence_double_vibro);
            notification_message(app->notif, &sequence_display_backlight_on);
        }
    } else {
        app->fails++;
        if(app->fails >= 2) app->conn = ConnOffline;
    }
    furi_mutex_release(app->mutex);
    view_port_update(app->vp);
}

static void send_decision(App* app) {
    char url[160], answer[8];
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    strncpy(answer, app->decision, sizeof(answer) - 1);
    answer[sizeof(answer) - 1] = 0;
    app->decision[0] = 0;
    furi_mutex_release(app->mutex);
    if(!answer[0]) return;
    snprintf(url, sizeof(url), "%s/decision?answer=%s", app->bridge, answer);
    bool ok = http_do(app, GET, url, NULL);
    FURI_LOG_I(TAG, "decision %s -> %s", answer, ok ? "ok" : "failed");
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    if(ok) {
        app->pending = false;
        strncpy(app->status, strcmp(answer, "allow") == 0 ? "allowed" : "denied", sizeof(app->status) - 1);
    }
    furi_mutex_release(app->mutex);
    notification_message(app->notif, ok ? &sequence_blink_green_100 : &sequence_blink_red_100);
    view_port_update(app->vp);
}

static int32_t worker(void* ctx) {
    App* app = ctx;
    // Board check: PING/PONG, then ask the board to (re)connect with its saved WiFi credentials.
    bool board = board_ping(app);
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    app->conn = board ? ConnOffline : ConnNoBoard;
    furi_mutex_release(app->mutex);
    view_port_update(app->vp);
    if(board) {
        char ssid[64] = {0}, pass[64] = {0};
        if(load_wifi(ssid, sizeof(ssid), pass, sizeof(pass))) {
            FURI_LOG_I(TAG, "pushing WiFi credentials for %s", ssid);
            flipper_http_save_wifi(app->fhttp, ssid, pass);
            furi_delay_ms(2000);
            app->fhttp->state = IDLE;
        }
        flipper_http_send_command(app->fhttp, HTTP_CMD_WIFI_CONNECT);
        furi_delay_ms(1500);
        app->fhttp->state = IDLE;
    }
    uint32_t reconnect_tries = 0;
    while(true) {
        uint32_t flags = furi_thread_flags_wait(EvtStop | EvtDecision, FuriFlagWaitAny, POLL_MS);
        if(flags & EvtStop) break;
        if(!board) {
            if(board_ping(app)) {   // board plugged in later
                board = true;
                flipper_http_send_command(app->fhttp, HTTP_CMD_WIFI_CONNECT);
                furi_delay_ms(1500);
                app->fhttp->state = IDLE;
            } else {
                continue;
            }
        }
        if(flags & EvtDecision) send_decision(app);
        poll_state(app);
        if(app->conn == ConnOffline && ++reconnect_tries % 10 == 0) {
            flipper_http_send_command(app->fhttp, HTTP_CMD_WIFI_CONNECT);   // nudge the board
            furi_delay_ms(500);
            app->fhttp->state = IDLE;
        }
    }
    return 0;
}

// ---------- UI ----------

static void draw_wrapped(Canvas* c, const char* text, uint8_t x, uint8_t y, uint8_t max_chars, uint8_t lines) {
    char buf[24];
    size_t len = strlen(text), pos = 0;
    for(uint8_t l = 0; l < lines && pos < len; l++) {
        size_t n = len - pos;
        if(n > max_chars) n = max_chars;
        memcpy(buf, text + pos, n);
        buf[n] = 0;
        canvas_draw_str(c, x, y + l * 10, buf);
        pos += n;
    }
}

static void draw(Canvas* c, void* ctx) {
    App* app = ctx;
    furi_mutex_acquire(app->mutex, FuriWaitForever);
    canvas_clear(c);

    // Header
    canvas_set_font(c, FontPrimary);
    canvas_draw_str(c, 2, 10, "CLAUDE CODE");
    canvas_set_font(c, FontSecondary);
    const char* conn = app->conn == ConnBoot    ? "boot" :
                       app->conn == ConnNoBoard ? "NO BOARD" :
                       app->conn == ConnOffline ? "no bridge" :
                                                  "online";
    canvas_draw_str_aligned(c, 126, 10, AlignRight, AlignBottom, conn);
    canvas_draw_line(c, 0, 13, 127, 13);

    // Body
    if(app->conn == ConnOnline) {
        char big[24];
        snprintf(big, sizeof(big), "%s", app->status);
        for(char* p = big; *p; p++) *p = (char)toupper((unsigned char)*p);
        canvas_set_font(c, FontPrimary);
        canvas_draw_str(c, 2, 27, big);
        canvas_set_font(c, FontSecondary);
        draw_wrapped(c, app->detail, 2, 39, 25, 2);
    } else {
        canvas_set_font(c, FontSecondary);
        if(app->conn == ConnNoBoard) {
            canvas_draw_str(c, 2, 27, "WiFi board not answering.");
            canvas_draw_str(c, 2, 38, "Plug it in (FlipperHTTP fw)");
        } else if(app->conn == ConnOffline) {
            canvas_draw_str(c, 2, 27, "Bridge unreachable:");
            draw_wrapped(c, app->bridge, 2, 38, 25, 1);
            draw_wrapped(c, app->last_line, 2, 48, 25, 1);
        } else {
            canvas_draw_str(c, 2, 27, "Starting...");
        }
    }

    // Footer
    if(app->pending && app->conn == ConnOnline) {
        canvas_draw_box(c, 0, 53, 128, 11);
        canvas_set_color(c, ColorWhite);
        canvas_draw_str_aligned(c, 64, 62, AlignCenter, AlignBottom, "OK = ALLOW    BACK = DENY");
        canvas_set_color(c, ColorBlack);
    } else {
        canvas_draw_str_aligned(c, 64, 62, AlignCenter, AlignBottom, "hold BACK to exit");
    }
    furi_mutex_release(app->mutex);
}

static void on_input(InputEvent* e, void* ctx) {
    furi_message_queue_put((FuriMessageQueue*)ctx, e, 0);
}

int32_t cc_monitor_app(void* p) {
    UNUSED(p);
    App* app = malloc(sizeof(App));
    memset(app, 0, sizeof(App));
    app->mutex = furi_mutex_alloc(FuriMutexTypeNormal);
    app->conn = ConnBoot;
    strncpy(app->status, "idle", sizeof(app->status) - 1);
    load_bridge_url(app);

    // The expansion-module service owns the UART by default; release it for FlipperHTTP.
    Expansion* expansion = furi_record_open(RECORD_EXPANSION);
    expansion_disable(expansion);

    app->notif = furi_record_open(RECORD_NOTIFICATION);
    app->fhttp = flipper_http_alloc();
    if(!app->fhttp) {
        FURI_LOG_E(TAG, "UART busy / FlipperHTTP alloc failed");
    } else {
        app->fhttp->user_rx_line_cb = rx_line;
        app->fhttp->user_callback_context = app;
    }

    FuriMessageQueue* q = furi_message_queue_alloc(8, sizeof(InputEvent));
    app->vp = view_port_alloc();
    view_port_draw_callback_set(app->vp, draw, app);
    view_port_input_callback_set(app->vp, on_input, q);
    Gui* gui = furi_record_open(RECORD_GUI);
    gui_add_view_port(gui, app->vp, GuiLayerFullscreen);

    if(app->fhttp) {
        app->worker = furi_thread_alloc_ex("CCMonWorker", 2048, worker, app);
        furi_thread_start(app->worker);
    } else {
        app->conn = ConnNoBoard;
    }

    InputEvent e;
    bool run = true;
    while(run) {
        if(furi_message_queue_get(q, &e, 100) != FuriStatusOk) continue;
        if(e.type == InputTypeShort || e.type == InputTypeRepeat) {
            furi_mutex_acquire(app->mutex, FuriWaitForever);
            bool pending = app->pending && app->conn == ConnOnline;
            if(pending && e.key == InputKeyOk) strcpy(app->decision, "allow");
            if(pending && e.key == InputKeyBack) strcpy(app->decision, "deny");
            bool queued = app->decision[0] != 0;
            furi_mutex_release(app->mutex);
            if(queued && app->worker) furi_thread_flags_set(furi_thread_get_id(app->worker), EvtDecision);
            view_port_update(app->vp);
        } else if(e.type == InputTypeLong && e.key == InputKeyBack) {
            run = false;
        }
    }

    if(app->worker) {
        furi_thread_flags_set(furi_thread_get_id(app->worker), EvtStop);
        furi_thread_join(app->worker);
        furi_thread_free(app->worker);
    }
    gui_remove_view_port(gui, app->vp);
    furi_record_close(RECORD_GUI);
    view_port_free(app->vp);
    furi_message_queue_free(q);
    if(app->fhttp) flipper_http_free(app->fhttp);
    furi_record_close(RECORD_NOTIFICATION);
    expansion_enable(expansion);
    furi_record_close(RECORD_EXPANSION);
    furi_mutex_free(app->mutex);
    free(app);
    return 0;
}
