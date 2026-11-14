// desktop.cc — native WebView2 shell for the light-agent desktop edition.
//
// The installed app is a single window (no console, no separate browser). On
// launch it:
//   1. spawns  light-agent.exe serve 18300  (hidden, output to light-agent-server.log)
//   2. opens the window IMMEDIATELY on an inline "starting" page — the boot
//      can legitimately take several seconds (restored MCP servers pay their
//      handshake timeouts before the HTTP listener comes up), and a blank
//      window used to read as "the app froze"
//   3. a worker thread polls for the HTTP server; once up the window
//      navigates to http://localhost:18300/, on timeout it shows an
//      in-window error page with recovery instructions
//   4. terminates the server when the window is closed
//
// Host implementation: raw WebView2 COM (the same pattern proven by the
// wv2test diagnostic host). The vendored webview.h (third_party/webview,
// v0.10.0) was dropped: on WebView2 runtime 153 its environment/controller
// wiring reliably wedges ALL main-frame navigation (window paints, renderer
// and JS evaluate work, but Page.navigate never starts) — reproduced with a
// minimal webview.h host and exhaustively bisected against the working raw
// COM host without finding a single-call difference. The raw COM host
// navigates in under 2 seconds every time.
//
// Build (package.sh does this). --subsystem=windows links the shell as a GUI
// binary so double-clicking it never pops a console window:
//   zig c++ -std=c++17 -O1 -I third_party/webview -o light-agent-desktop.exe tools/desktop.cc \
//     -Wl,--subsystem=windows -lole32 -loleaut32 -luuid -lshlwapi -luser32 -lgdi32 -lws2_32 -lshell32 -ladvapi32 -lwindowscodecs

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <windowsx.h>
#include <shellapi.h>
#include <shlwapi.h>
#include <unknwn.h>
#include <wincodec.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "WebView2.h"

static const int kPort = 18300;
static const int kChromeHeight = 40;
static UINT g_dpi = 96;
static HWND g_caption_buttons[3] = {};
static WNDPROC g_button_proc = nullptr;
static HWND g_hover_button = nullptr;
static int scaled(int value) { return MulDiv(value, g_dpi, 96); }
static int chrome_height() { return scaled(kChromeHeight); }
static const UINT WM_APP_NAV = WM_APP + 1; /* wParam 1 = console, 2 = error page */

static HWND g_hwnd = nullptr;
static ICoreWebView2 *g_web = nullptr;
static bool g_startup_animation = true;
static bool g_startup_battle = false;
static constexpr UINT_PTR kStartupFinishTimer = 41;
static ICoreWebView2Controller *g_ctrl = nullptr;
static HANDLE g_server = nullptr;
static HICON g_icon_big = nullptr;
static HICON g_icon_small = nullptr;
static std::wstring g_url_main;
static std::wstring g_url_startup;
static std::wstring g_url_error;
static HWND g_task_pet = nullptr;
static std::wstring g_task_pet_title;
static std::wstring g_task_pet_status;

static void publish_task_pet_state() {
    if (g_web) g_web->PostWebMessageAsString(g_task_pet ? L"task-pet:open" : L"task-pet:closed");
}

static std::wstring g_task_pet_asset;
static std::wstring g_dragon_gif, g_dragon_sheet, g_dragon_mount;
static bool g_task_pet_use_dragon = false;
static int g_task_pet_zoom = 100;
struct TaskPetSprite {
    std::vector<uint32_t> pixels;
    UINT width = 0;
    UINT height = 0;
};
enum class TaskPetMood { Smile, Think, Move, Celebrate };
static TaskPetSprite g_task_pet_smile, g_task_pet_thinking, g_task_pet_dragon, g_task_pet_mount;
static TaskPetMood g_task_pet_mood = TaskPetMood::Smile;
static ULONGLONG g_task_pet_mood_since = 0;
static bool g_task_pet_animate = false;

static void set_task_pet_mood(TaskPetMood mood) {
    if (g_task_pet_mood != mood) {
        g_task_pet_mood = mood;
        g_task_pet_mood_since = GetTickCount64();
    }
}

static bool load_task_pet_sprite(const std::wstring &path, TaskPetSprite &sprite, int zoom = 100) {
    IWICImagingFactory *factory = nullptr;
    IWICBitmapDecoder *decoder = nullptr;
    IWICBitmapFrameDecode *frame = nullptr;
    IWICBitmapScaler *scaler = nullptr;
    IWICFormatConverter *converter = nullptr;
    UINT width = 0, height = 0;
    HRESULT result = CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                                      IID_IWICImagingFactory, reinterpret_cast<void **>(&factory));
    if (SUCCEEDED(result))
        result = factory->CreateDecoderFromFilename(path.c_str(), nullptr, GENERIC_READ,
                                                    WICDecodeMetadataCacheOnLoad, &decoder);
    if (SUCCEEDED(result)) result = decoder->GetFrame(0, &frame);
    if (SUCCEEDED(result)) result = frame->GetSize(&width, &height);
    if (SUCCEEDED(result) && (!width || !height)) result = E_FAIL;
    if (SUCCEEDED(result)) result = factory->CreateFormatConverter(&converter);
    if (SUCCEEDED(result))
        result = converter->Initialize(frame, GUID_WICPixelFormat32bppPBGRA,
                                        WICBitmapDitherTypeNone, nullptr, 0, WICBitmapPaletteTypeCustom);
    if (SUCCEEDED(result)) result = factory->CreateBitmapScaler(&scaler);
    if (SUCCEEDED(result)) {
        const UINT edge = (std::max)(width, height);
        width = (std::max)(1, MulDiv(width, MulDiv(scaled(150), zoom, 100), edge));
        height = (std::max)(1, MulDiv(height, MulDiv(scaled(150), zoom, 100), edge));
        result = scaler->Initialize(converter, width, height, WICBitmapInterpolationModeFant);
    }
    if (SUCCEEDED(result)) {
        sprite.pixels.resize(width * height);
        result = scaler->CopyPixels(nullptr, width * 4, width * height * 4,
                                    reinterpret_cast<BYTE *>(sprite.pixels.data()));
    }
    if (scaler) scaler->Release();
    if (converter) converter->Release();
    if (frame) frame->Release();
    if (decoder) decoder->Release();
    if (factory) factory->Release();
    if (FAILED(result)) {
        sprite = {};
        return false;
    }
    sprite.width = width;
    sprite.height = height;
    return true;
}

static bool render_task_pet(HWND hwnd) {
    RECT bounds{};
    GetClientRect(hwnd, &bounds);
    const int width = bounds.right;
    const int height = bounds.bottom;
    const bool dragon = g_task_pet_use_dragon && !g_task_pet_dragon.pixels.empty();
    const bool working = g_task_pet_mood == TaskPetMood::Think || g_task_pet_mood == TaskPetMood::Move;
    const TaskPetSprite &sprite = dragon
        ? (working && !g_task_pet_mount.pixels.empty() ? g_task_pet_mount : g_task_pet_dragon)
        : (g_task_pet_mood == TaskPetMood::Think && !g_task_pet_thinking.pixels.empty()
            ? g_task_pet_thinking : g_task_pet_smile);
    const UINT sprite_height = sprite.height;
    if (width <= 0 || height <= 0 || sprite.pixels.empty()) return false;
    const auto fit = [height](int value) { return MulDiv(value, height, 228); };
    BITMAPINFO info{};
    info.bmiHeader.biSize = sizeof(BITMAPINFOHEADER);
    info.bmiHeader.biWidth = width;
    info.bmiHeader.biHeight = -height;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    void *memory = nullptr;
    HDC dc = CreateCompatibleDC(nullptr);
    if (!dc) return false;
    HBITMAP bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, &memory, nullptr, 0);
    if (!bitmap) {
        DeleteDC(dc);
        return false;
    }
    HGDIOBJ previous = SelectObject(dc, bitmap);
    auto *pixels = static_cast<uint32_t *>(memory);
    std::memset(pixels, 0, static_cast<size_t>(width) * height * sizeof(*pixels));

    RECT caption{fit(7), fit(168), width - fit(7), fit(221)};
    HBRUSH brush = CreateSolidBrush(RGB(30, 27, 51));
    HGDIOBJ old_brush = SelectObject(dc, brush);
    SelectObject(dc, GetStockObject(NULL_PEN));
    RoundRect(dc, caption.left, caption.top, caption.right, caption.bottom, fit(18), fit(18));
    SelectObject(dc, old_brush);
    DeleteObject(brush);
    SetBkMode(dc, TRANSPARENT);
    SetTextColor(dc, RGB(241, 235, 255));
    HGDIOBJ old_font = SelectObject(dc, GetStockObject(DEFAULT_GUI_FONT));
    RECT status = caption;
    status.left += fit(12);
    status.right -= fit(12);
    if (!g_task_pet_title.empty()) {
        RECT title = status;
        title.bottom = title.top + fit(28);
        HFONT bold = CreateFontW(-fit(15), 0, 0, 0, FW_BOLD, FALSE, FALSE, FALSE,
                                 DEFAULT_CHARSET, 0, 0, DEFAULT_QUALITY, 0, nullptr);
        if (bold) SelectObject(dc, bold);
        DrawTextW(dc, g_task_pet_title.c_str(), -1, &title,
                  DT_LEFT | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX | DT_END_ELLIPSIS);
        if (bold) {
            SelectObject(dc, GetStockObject(DEFAULT_GUI_FONT));
            DeleteObject(bold);
        }
        status.top = title.bottom - fit(3);
        SetTextColor(dc, RGB(192, 187, 211));
    }
    DrawTextW(dc, g_task_pet_status.c_str(), -1, &status,
              (g_task_pet_title.empty() ? DT_CENTER : DT_LEFT) | DT_VCENTER | DT_SINGLELINE |
              DT_NOPREFIX | DT_END_ELLIPSIS);
    SelectObject(dc, old_font);
    GdiFlush();
    // GDI paints the caption RGB. Restore its alpha before adding the already
    // premultiplied sprite so the mascot's soft edges remain transparent.
    for (int y = caption.top; y < caption.bottom; ++y)
        for (int x = caption.left; x < caption.right; ++x)
            if (pixels[y * width + x] & 0x00ffffffu) pixels[y * width + x] |= 0xff000000u;

    int bob = 0, sway = 0;
    if (g_task_pet_animate) {
        const ULONGLONG elapsed = GetTickCount64() - g_task_pet_mood_since;
        if (dragon) {
            if (elapsed < 360 && g_task_pet_mood == TaskPetMood::Celebrate)
                bob = -static_cast<int>(fit(4) * std::sin(elapsed * (3.141592654 / 360)));
        } else if (g_task_pet_mood == TaskPetMood::Move) {
            const double phase = (elapsed % 1100) * (6.283185307 / 1100);
            bob = -static_cast<int>(fit(7) * std::abs(std::sin(phase)));
            sway = static_cast<int>(fit(7) * std::cos(phase));
        } else if (g_task_pet_mood == TaskPetMood::Celebrate && elapsed < 1050) {
            const double phase = (elapsed % 525) * (3.141592654 / 525);
            bob = -static_cast<int>(fit(8) * std::sin(phase));
        } else {
            const int period = g_task_pet_mood == TaskPetMood::Think ? 4800 : 3800;
            bob = static_cast<int>(fit(3) * std::sin((elapsed % period) * (6.283185307 / period)));
        }
    }
    const int left = (width - static_cast<int>(sprite.width)) / 2 + sway;
    const int top = (std::max)(0, fit(10) + bob);
    if (left >= 0 &&
        left + static_cast<int>(sprite.width) <= width &&
        top >= 0 && top + static_cast<int>(sprite_height) <= caption.top) {
        for (UINT row = 0; row < sprite_height; ++row) {
            auto *dest = pixels + (top + row) * width + left;
            const auto *source = sprite.pixels.data() + row * sprite.width;
            std::memcpy(dest, source, sprite.width * sizeof(*pixels));

        }
    }

    SIZE size{width, height};
    POINT source{};
    BLENDFUNCTION blend{AC_SRC_OVER, 0, 255, AC_SRC_ALPHA};
    const bool ok = UpdateLayeredWindow(hwnd, nullptr, nullptr, &size, dc, &source, 0, &blend, ULW_ALPHA) != FALSE;
    SelectObject(dc, previous);
    DeleteObject(bitmap);
    DeleteDC(dc);
    return ok;
}

static void task_pet_open_main(const wchar_t *message) {
    if (!g_hwnd || !g_web) return;
    if (IsIconic(g_hwnd)) ShowWindow(g_hwnd, SW_RESTORE);
    SetForegroundWindow(g_hwnd);
    if (g_ctrl) g_ctrl->MoveFocus(COREWEBVIEW2_MOVE_FOCUS_REASON_PROGRAMMATIC);
    g_web->PostWebMessageAsString(message);
}

static LRESULT CALLBACK task_pet_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    static POINT pressed_at{};
    static RECT initial_bounds{};
    static bool dragged = false;
    if (msg == WM_TIMER && wp == 1) {
        render_task_pet(hwnd);
        return 0;
    }
    if (msg == WM_LBUTTONDOWN) {
        pressed_at = POINT{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
        ClientToScreen(hwnd, &pressed_at);
        GetWindowRect(hwnd, &initial_bounds);
        dragged = false;
        SetCapture(hwnd);
        return 0;
    }
    if ((msg == WM_MOUSEMOVE || msg == WM_LBUTTONUP) && GetCapture() == hwnd) {
        POINT point{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
        ClientToScreen(hwnd, &point);
        const int dx = point.x - pressed_at.x;
        const int dy = point.y - pressed_at.y;
        dragged = dragged || std::abs(dx) >= GetSystemMetrics(SM_CXDRAG)
                          || std::abs(dy) >= GetSystemMetrics(SM_CYDRAG);
        if (dragged)
            SetWindowPos(hwnd, nullptr, initial_bounds.left + dx, initial_bounds.top + dy,
                         0, 0, SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
        if (msg == WM_LBUTTONUP) {
            ReleaseCapture();
            if (!dragged) task_pet_open_main(L"task-pet:recent-task");
        }
        return 0;
    }
    if (msg == WM_MOUSEWHEEL) {
        SendMessageW(hwnd, WM_COMMAND, GET_WHEEL_DELTA_WPARAM(wp) > 0 ? 5 : 6, 0);
        return 0;
    }
    if (msg == WM_COMMAND && (wp == 5 || wp == 6)) {
        const int zoom = (std::max)(60, (std::min)(160, g_task_pet_zoom + (wp == 5 ? 10 : -10)));
        if (zoom == g_task_pet_zoom) return 0;
        TaskPetSprite smile, thinking, dragon, mount;
        if (!load_task_pet_sprite(g_task_pet_asset, smile, zoom)) return 0;
        const auto separator = g_task_pet_asset.find_last_of(L"\\/");
        load_task_pet_sprite(g_task_pet_asset.substr(0, separator + 1) + L"light-agent-pet-thinking.png", thinking, zoom);
        if (!load_task_pet_sprite(g_dragon_sheet, dragon, zoom) && g_task_pet_use_dragon) return 0;
        g_task_pet_smile = std::move(smile);
        g_task_pet_thinking = std::move(thinking);
        load_task_pet_sprite(g_dragon_mount, mount, zoom);
        g_task_pet_dragon = std::move(dragon);
        g_task_pet_mount = std::move(mount);
        g_task_pet_zoom = zoom;
        const int width = MulDiv(scaled(260), zoom, 100), height = MulDiv(scaled(228), zoom, 100);
        RECT bounds{}, work{};
        GetWindowRect(hwnd, &bounds);
        MONITORINFO monitor{sizeof(MONITORINFO)};
        if (GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), &monitor)) work = monitor.rcWork;
        else SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
        const int left = (std::max)(work.left, (std::min)(bounds.left, work.right - width));
        const int top = (std::max)(work.top, (std::min)(bounds.top, work.bottom - height));
        SetWindowPos(hwnd, nullptr, left, top, width, height, SWP_NOZORDER | SWP_NOACTIVATE);
        render_task_pet(hwnd);
        return 0;
    }
    if (msg == WM_CONTEXTMENU) {
        POINT point{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)};
        if (point.x == -1 && point.y == -1) GetCursorPos(&point);
        HMENU menu = CreatePopupMenu();
        if (menu) {
            AppendMenuW(menu, MF_STRING, 1, L"新建聊天");
            AppendMenuW(menu, MF_STRING, 2, L"关闭桌宠");
            AppendMenuW(menu, MF_SEPARATOR, 0, nullptr);
            AppendMenuW(menu, MF_STRING | (!g_task_pet_use_dragon ? MF_CHECKED : 0), 3, L"经典机器人");
            AppendMenuW(menu, MF_STRING | (g_task_pet_use_dragon ? MF_CHECKED : 0) |
                (g_task_pet_dragon.pixels.empty() ? MF_GRAYED : 0), 4, L"龙女");
            AppendMenuW(menu, MF_STRING, 5, L"放大");
            AppendMenuW(menu, MF_STRING, 6, L"缩小");
            SetForegroundWindow(hwnd);
            const UINT choice = TrackPopupMenu(menu, TPM_RETURNCMD | TPM_NONOTIFY,
                                               point.x, point.y, 0, hwnd, nullptr);
            DestroyMenu(menu);
            if (choice == 1) task_pet_open_main(L"task-pet:new-chat");
            if (choice == 2) DestroyWindow(hwnd);
            if (choice == 3 || choice == 4) {
                g_task_pet_use_dragon = choice == 4;
                render_task_pet(hwnd);
            }
            if (choice == 5 || choice == 6) SendMessageW(hwnd, WM_COMMAND, choice, 0);

        }
        return 0;
    }
    if (msg == WM_NCDESTROY) {
        KillTimer(hwnd, 1);
        g_task_pet = nullptr;
        g_task_pet_smile = {};
        g_task_pet_thinking = {};
        g_task_pet_dragon = {};
        g_task_pet_mount = {};
        publish_task_pet_state();
    }
    return DefWindowProcW(hwnd, msg, wp, lp);
}

static void set_task_pet_enabled(bool enabled) {
    if (!enabled || g_task_pet) {
        if (g_task_pet && !enabled && DestroyWindow(g_task_pet)) return;
        publish_task_pet_state();
        return;
    }
    if (!load_task_pet_sprite(g_task_pet_asset, g_task_pet_smile, g_task_pet_zoom)) {
        if (g_web) g_web->PostWebMessageAsString(L"task-pet:unavailable");
        publish_task_pet_state();
        return;
    }
    const size_t separator = g_task_pet_asset.find_last_of(L"\\/");
    const std::wstring asset_dir = separator == std::wstring::npos ? L"" : g_task_pet_asset.substr(0, separator + 1);
    load_task_pet_sprite(asset_dir + L"light-agent-pet-thinking.png", g_task_pet_thinking, g_task_pet_zoom);
    load_task_pet_sprite(g_dragon_sheet, g_task_pet_dragon, g_task_pet_zoom);
    load_task_pet_sprite(g_dragon_mount, g_task_pet_mount, g_task_pet_zoom);
    RECT work{};
    SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0);
    WNDCLASSW window_class{};
    window_class.style = CS_DBLCLKS;
    window_class.lpfnWndProc = task_pet_proc;
    window_class.hInstance = GetModuleHandleW(nullptr);
    window_class.lpszClassName = L"LightAgentTaskPet";
    window_class.hCursor = LoadCursorA(nullptr, IDC_HAND);
    RegisterClassW(&window_class);
    g_task_pet_title.clear();
    set_task_pet_mood(TaskPetMood::Smile);
    g_task_pet_status = L"正在获取任务状态…";
    g_task_pet = CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED, window_class.lpszClassName,
        L"light-agent · 任务状态", WS_POPUP,
        work.right - MulDiv(scaled(276), g_task_pet_zoom, 100), work.bottom - MulDiv(scaled(244), g_task_pet_zoom, 100),
        MulDiv(scaled(260), g_task_pet_zoom, 100), MulDiv(scaled(228), g_task_pet_zoom, 100),
        nullptr, nullptr, window_class.hInstance, nullptr);
    if (g_task_pet && render_task_pet(g_task_pet)) {
        BOOL animate = TRUE;
        SystemParametersInfoW(SPI_GETCLIENTAREAANIMATION, 0, &animate, 0);
        g_task_pet_animate = animate != FALSE;
        if (g_task_pet_animate) SetTimer(g_task_pet, 1, 40, nullptr);
        ShowWindow(g_task_pet, SW_SHOWNOACTIVATE);
    } else {
        if (g_web) g_web->PostWebMessageAsString(L"task-pet:unavailable");
        if (g_task_pet) DestroyWindow(g_task_pet);
        g_task_pet_smile = {};
        g_task_pet_thinking = {};
        g_task_pet_dragon = {};
        g_task_pet_mount = {};
    }
    publish_task_pet_state();
}

class TaskPetHandler final : public ICoreWebView2WebMessageReceivedEventHandler {
    LONG refs_ = 1;
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID iid, void **out) override {
        if (!out) return E_POINTER;
        *out = nullptr;
        if (iid != IID_IUnknown && iid != IID_ICoreWebView2WebMessageReceivedEventHandler)
            return E_NOINTERFACE;
        *out = static_cast<ICoreWebView2WebMessageReceivedEventHandler *>(this);
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&refs_); }
    ULONG STDMETHODCALLTYPE Release() override {
        const ULONG refs = InterlockedDecrement(&refs_);
        if (!refs) delete this;
        return refs;
    }
    HRESULT STDMETHODCALLTYPE Invoke(ICoreWebView2 *, ICoreWebView2WebMessageReceivedEventArgs *args) override {
        LPWSTR source = nullptr, message = nullptr;
        if (SUCCEEDED(args->get_Source(&source)) && source && g_url_main == source &&
            SUCCEEDED(args->TryGetWebMessageAsString(&message)) && message) {
            if (g_task_pet && !IsWindow(g_task_pet)) g_task_pet = nullptr;
            if (!wcscmp(message, L"task-pet:classic") || !wcscmp(message, L"task-pet:dragon")) {
                g_task_pet_use_dragon = !wcscmp(message, L"task-pet:dragon");
                if (g_task_pet) render_task_pet(g_task_pet);
            }
            else if (!wcscmp(message, L"task-pet:open")) set_task_pet_enabled(true);
            else if (!wcscmp(message, L"task-pet:close")) set_task_pet_enabled(false);
            else if (!wcscmp(message, L"task-pet:query")) publish_task_pet_state();
            else if (!wcscmp(message, L"task-pet:toggle")) set_task_pet_enabled(!g_task_pet);
            else if (g_task_pet && !wcsncmp(message, L"task-pet:status:", 16) && wcslen(message) < 400) {
                const wchar_t *status = message + 16;
                const wchar_t *line = wcschr(status, L'\n');
                const wchar_t *next_line = line ? wcschr(line + 1, L'\n') : nullptr;
                TaskPetMood mood = TaskPetMood::Smile;
                if (next_line) {
                    const std::wstring mode(status, line);
                    if (mode == L"think") mood = TaskPetMood::Think;
                    else if (mode == L"move") mood = TaskPetMood::Move;
                    else if (mode == L"celebrate") mood = TaskPetMood::Celebrate;
                    status = line + 1;
                    line = next_line;
                }
                set_task_pet_mood(mood);
                g_task_pet_title = line ? std::wstring(status, line) : L"";
                g_task_pet_status = line ? line + 1 : status;
                render_task_pet(g_task_pet);
            }
        }
        CoTaskMemFree(source);
        CoTaskMemFree(message);
        return S_OK;
    }
};

/* ---- ICoreWebView2EnvironmentOptions: pass --disable-features to the
 * browser process.  The WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS env var is
 * honored by the runtime too, but the options object is the supported
 * channel; both are set (the env var preserves any user-provided value). */
class DisableFeaturesOptions : public ICoreWebView2EnvironmentOptions {
    LONG ref_ = 1;
    std::wstring args_;
public:
    explicit DisableFeaturesOptions(const std::wstring &args) : args_(args) {}
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_ICoreWebView2EnvironmentOptions)
            *ppv = static_cast<ICoreWebView2EnvironmentOptions *>(this);
        else
            return E_NOINTERFACE;
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG r = InterlockedDecrement(&ref_);
        if (!r) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE get_AdditionalBrowserArguments(LPWSTR *v) override {
        if (!v) return E_POINTER;
        *v = (LPWSTR)CoTaskMemAlloc((args_.size() + 1) * sizeof(wchar_t));
        if (*v) wcscpy(*v, args_.c_str());
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE put_AdditionalBrowserArguments(LPCWSTR v) override { args_ = v ? v : L""; return S_OK; }
    HRESULT STDMETHODCALLTYPE get_Language(LPWSTR *v) override {
        if (!v) return E_POINTER;
        *v = (LPWSTR)CoTaskMemAlloc(6 * sizeof(wchar_t));
        if (*v) wcscpy(*v, L"zh-CN");
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE put_Language(LPCWSTR) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE get_TargetCompatibleBrowserVersion(LPWSTR *v) override {
        if (!v) return E_POINTER;
        *v = (LPWSTR)CoTaskMemAlloc(14 * sizeof(wchar_t));
        if (*v) wcscpy(*v, L"120.0.0.0");
        return S_OK;
    }
    HRESULT STDMETHODCALLTYPE put_TargetCompatibleBrowserVersion(LPCWSTR) override { return S_OK; }
    HRESULT STDMETHODCALLTYPE get_AllowSingleSignOnUsingOSPrimaryAccount(BOOL *v) override { if (v) *v = FALSE; return S_OK; }
    HRESULT STDMETHODCALLTYPE put_AllowSingleSignOnUsingOSPrimaryAccount(BOOL) override { return S_OK; }
};

/* ---- completion handlers ---- */
class EnvHandler : public ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler {
    LONG ref_ = 1;
public:
    bool done = false;
    HRESULT hr = E_FAIL;
    ICoreWebView2Environment *env = nullptr;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler)
            *ppv = static_cast<ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler *>(this);
        else
            return E_NOINTERFACE;
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG r = InterlockedDecrement(&ref_);
        if (!r) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT errorCode, ICoreWebView2Environment *environment) override {
        hr = errorCode;
        env = environment;
        if (env) env->AddRef();
        done = true;
        return S_OK;
    }
};

class CtrlHandler : public ICoreWebView2CreateCoreWebView2ControllerCompletedHandler {
    LONG ref_ = 1;
public:
    bool done = false;
    HRESULT hr = E_FAIL;
    ICoreWebView2Controller *ctrl = nullptr;
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_ICoreWebView2CreateCoreWebView2ControllerCompletedHandler)
            *ppv = static_cast<ICoreWebView2CreateCoreWebView2ControllerCompletedHandler *>(this);
        else
            return E_NOINTERFACE;
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG r = InterlockedDecrement(&ref_);
        if (!r) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE Invoke(HRESULT errorCode, ICoreWebView2Controller *controller) override {
        hr = errorCode;
        ctrl = controller;
        if (ctrl) ctrl->AddRef();
        done = true;
        return S_OK;
    }
};

/* ---- external link handlers ----
 * Market cards link to github.com and the model plaza links to provider
 * sites. Without these handlers WebView2 either navigates the whole console
 * away or silently drops target=_blank requests, so the repo links in the
 * plugins/skills markets were not clickable inside the desktop shell.
 * External URLs now open in the user's default browser instead. */

static bool is_local_url(LPCWSTR url) {
    return url && (wcsncmp(url, L"http://localhost", 16) == 0 ||
                   wcsncmp(url, L"http://127.0.0.1", 15) == 0 ||
                   wcsncmp(url, L"data:", 5) == 0 ||
                   wcsncmp(url, L"about:", 6) == 0);
}

class NavStartHandler : public ICoreWebView2NavigationStartingEventHandler {
    LONG ref_ = 1;
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_ICoreWebView2NavigationStartingEventHandler)
            *ppv = static_cast<ICoreWebView2NavigationStartingEventHandler *>(this);
        else
            return E_NOINTERFACE;
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG r = InterlockedDecrement(&ref_);
        if (!r) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE Invoke(ICoreWebView2 *sender,
                                     ICoreWebView2NavigationStartingEventArgs *args) override {
        (void)sender;
        if (!args) return S_OK;
        LPWSTR uri = nullptr;
        if (SUCCEEDED(args->get_Uri(&uri)) && uri) {
            /* External top-level navigation must never replace the console. */
            if (!is_local_url(uri) && g_url_startup != uri) {
                ShellExecuteW(nullptr, L"open", uri, nullptr, nullptr, SW_SHOWNORMAL);
                args->put_Cancel(TRUE);
            }
            CoTaskMemFree(uri);
        }
        return S_OK;
    }
};

class NewWindowHandler : public ICoreWebView2NewWindowRequestedEventHandler {
    LONG ref_ = 1;
public:
    HRESULT STDMETHODCALLTYPE QueryInterface(REFIID riid, void **ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == IID_IUnknown || riid == IID_ICoreWebView2NewWindowRequestedEventHandler)
            *ppv = static_cast<ICoreWebView2NewWindowRequestedEventHandler *>(this);
        else
            return E_NOINTERFACE;
        AddRef();
        return S_OK;
    }
    ULONG STDMETHODCALLTYPE AddRef() override { return InterlockedIncrement(&ref_); }
    ULONG STDMETHODCALLTYPE Release() override {
        ULONG r = InterlockedDecrement(&ref_);
        if (!r) delete this;
        return r;
    }
    HRESULT STDMETHODCALLTYPE Invoke(ICoreWebView2 *sender,
                                     ICoreWebView2NewWindowRequestedEventArgs *args) override {
        (void)sender;
        if (!args) return S_OK;
        LPWSTR uri = nullptr;
        if (SUCCEEDED(args->get_Uri(&uri)) && uri) {
            if (!is_local_url(uri))
                ShellExecuteW(nullptr, L"open", uri, nullptr, nullptr, SW_SHOWNORMAL);
            CoTaskMemFree(uri);
        }
        args->put_Handled(TRUE);
        return S_OK;
    }
};

/* Load the runtime's own loader DLL (installed evergreen runtime). */
using CreateWebViewEnvironmentWithOptionsInternal_t =
    HRESULT(STDMETHODCALLTYPE *)(bool, int, PCWSTR, IUnknown *,
                                 ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler *);

static HMODULE g_runtime_dll = nullptr;
static CreateWebViewEnvironmentWithOptionsInternal_t g_create_env = nullptr;

static bool load_runtime() {
    char ver[64] = {0};
    DWORD sz = sizeof(ver);
    HKEY key;
    if (RegOpenKeyA(HKEY_LOCAL_MACHINE,
                    "SOFTWARE\\WOW6432Node\\Microsoft\\EdgeUpdate\\Clients"
                    "\\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
                    &key) == ERROR_SUCCESS) {
        if (RegQueryValueExA(key, "pv", nullptr, nullptr, (BYTE *)ver, &sz) != ERROR_SUCCESS)
            ver[0] = 0;
        RegCloseKey(key);
    }
    char path[MAX_PATH] = {0};
    if (ver[0]) {
        snprintf(path, sizeof(path),
                 "C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\\%s\\EBWebView\\x64\\EmbeddedBrowserWebView.dll", ver);
        g_runtime_dll = LoadLibraryA(path);
    }
    if (!g_runtime_dll) {
        /* fallback: scan the Application dir for any version */
        WIN32_FIND_DATAA fd;
        HANDLE h = FindFirstFileA("C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\\*.*", &fd);
        if (h != INVALID_HANDLE_VALUE) {
            do {
                if (fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY && fd.cFileName[0] >= '0' && fd.cFileName[0] <= '9') {
                    snprintf(path, sizeof(path),
                             "C:\\Program Files (x86)\\Microsoft\\EdgeWebView\\Application\\%s\\EBWebView\\x64\\EmbeddedBrowserWebView.dll", fd.cFileName);
                    g_runtime_dll = LoadLibraryA(path);
                    if (g_runtime_dll) break;
                }
            } while (FindNextFileA(h, &fd));
            FindClose(h);
        }
    }
    if (!g_runtime_dll) return false;
    g_create_env = (CreateWebViewEnvironmentWithOptionsInternal_t)(void *)GetProcAddress(
        g_runtime_dll, "CreateWebViewEnvironmentWithOptionsInternal");
    return g_create_env != nullptr;
}

/* Pump messages until *flag turns true (or timeout_ms elapses). */
static bool pump_until(bool *flag, int timeout_ms) {
    DWORD start = GetTickCount();
    MSG msg;
    while (!*flag) {
        while (PeekMessageA(&msg, nullptr, 0, 0, PM_REMOVE)) {
            if (msg.message == WM_QUIT) return false;
            TranslateMessage(&msg);
            DispatchMessageA(&msg);
        }
        if (*flag) break;
        if (GetTickCount() - start > (DWORD)timeout_ms) return false;
        WaitMessage();
    }
    return true;
}

static std::wstring exe_dir() {
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(nullptr, path, MAX_PATH);
    PathRemoveFileSpecW(path);
    return std::wstring(path);
}

/* Poll the local HTTP server until it accepts a connection or we time out. */
static bool wait_server(int port, int timeout_ms) {
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return false;

    sockaddr_in addr;
    std::memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons((u_short)port);
    addr.sin_addr.s_addr = inet_addr("127.0.0.1");

    ULONGLONG start = GetTickCount64();
    bool up = false;
    while (GetTickCount64() - start < (ULONGLONG)timeout_ms) {
        SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (s != INVALID_SOCKET) {
            if (connect(s, (sockaddr *)&addr, sizeof(addr)) == 0) {
                up = true;
            }
            closesocket(s);
        }
        if (up) break;
        Sleep(200);
    }
    WSACleanup();
    return up;
}

/* Inline HTML pages shown while the backend boots / if it never comes up. */
static const char kStartingHtml[] = R"START(<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
*{box-sizing:border-box}body{margin:0;background:#100a10;color:#fff4f8;font:15px system-ui;overflow:hidden}
.startup-media,.aura{position:fixed;inset:0;width:100%;height:100%;object-fit:contain}
.startup-media{filter:brightness(.55) saturate(.8);transition:filter .45s,transform .7s}
.aura{pointer-events:none;background:radial-gradient(ellipse,#ff294a40,transparent 60%);mix-blend-mode:screen;animation:pulse 2s ease-in-out infinite alternate}
@keyframes pulse{from{opacity:.25;transform:scale(.8)}to{opacity:.8;transform:scale(1.15)}}
.startup-status{position:fixed;left:50%;bottom:7vh;transform:translateX(-50%);width:min(440px,90vw);padding:20px 24px;border:1px solid #e7577660;border-radius:16px;background:#170e18ed;box-shadow:0 0 36px #ed365035}
header{display:flex;justify-content:space-between;gap:12px}p{margin:12px 0 0;color:#f6c7d5}.track{margin-top:15px;background:#4a2437;border-radius:8px;height:6px;overflow:hidden}
.fill{width:12%;height:100%;background:linear-gradient(90deg,#b52346,#ff819c);box-shadow:0 0 12px #ff5977;transition:width .28s ease}
.ready .startup-media{filter:brightness(1) saturate(1.2);transform:scale(1.035)}
.ready .aura{animation:flash .65s ease-out both}@keyframes flash{0%{opacity:0}28%{opacity:1;background:#ffc2d399}100%{opacity:.16}}
.startup-hero,.battle-shot{position:fixed;inset:4vh 0 auto;width:100%;height:68vh;object-fit:contain;pointer-events:none}
.battle-shot{opacity:0}
.startup-dragon{position:fixed;left:50%;top:32%;width:min(24vw,230px);overflow:visible;pointer-events:none;animation:orbit 6s linear infinite}
.startup-dragon .fire{transform-origin:150px 55px;animation:breath 6s linear infinite}
@keyframes orbit{0%,100%{transform:translate(-43vw,-9vh) scale(.58);z-index:0}24%{transform:translate(19vw,-21vh) scale(.75);z-index:0}49%{transform:translate(22vw,8vh) scaleX(-1);z-index:2}75%{transform:translate(-35vw,17vh) scaleX(-.82);z-index:2}}
@keyframes breath{0%,26%,48%,70%,93%,100%{opacity:0;transform:scaleX(.15)}32%,78%{opacity:1;transform:scaleX(1)}}
.ready .startup-hero{animation:ascend 1.05s ease-in both}
.ready .startup-dragon{visibility:hidden}
.startup-status{z-index:3}
.ready:has(.battle-shot) .startup-media{animation:ascend 1.05s ease-in both}
.ready .battle-shot{animation:charge 2.2s .94s ease-out both}
.ready:has(.battle-shot) .aura{animation:flash .7s .69s ease-out both,flash .45s 2.32s ease-out both}
@keyframes ascend{0%{opacity:1}65%{opacity:1;filter:brightness(2.7) saturate(1.6)}100%{opacity:0;transform:translateY(-35vh) scale(1.2)}}
@keyframes charge{0%{opacity:0;transform:translateY(8vh) scale(1.2);filter:brightness(2.3)}25%{opacity:1;transform:translateY(0) scale(1.06);filter:brightness(1)}100%{opacity:1;transform:scale(1.17)}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
@media(prefers-reduced-motion:reduce){.startup-dragon,.ready .startup-hero{display:none}.ready .battle-shot{opacity:1}}
</style></head><body><div class="aura"></div><main class="startup-status" role="status">
<header><strong>light-agent</strong><span id="percent">12%</span></header>
<div class="track" id="track" role="progressbar" aria-label="启动进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="12"><div class="fill" id="fill"></div></div>
<p id="stage">正在初始化桌面环境…</p></main>
<script>
const phases=[[12,'正在初始化桌面环境…'],[38,'正在连接本地运行时…'],[72,'等待运行时就绪 · 龙女正在蓄力'],[100,'运行时已就绪 · 变身完成']];
let phase=0;
function startupPhase(next){
  if(next<=phase)return;
  phase=next;
  const [value,label]=phases[phase];
  document.getElementById('fill').style.width=value+'%';
  document.getElementById('percent').textContent=value+'%';
  document.getElementById('track').setAttribute('aria-valuenow',value);
  document.getElementById('stage').textContent=label;
  if(phase===3){
    document.body.classList.add('ready');
    if(document.querySelector('.battle-shot') && !matchMedia('(prefers-reduced-motion: reduce)').matches){
      const stage=document.getElementById('stage');
      stage.textContent='能量释放 · 龙女升空';
      setTimeout(()=>stage.textContent='战斗形态觉醒 · 骑龙出击',1050);
      setTimeout(()=>stage.textContent='变身完成 · 运行时已就绪',2530);
    }
  }
}
setTimeout(()=>startupPhase(1),180);
setTimeout(()=>startupPhase(2),1100);
</script></body></html>)START";

static const char kErrorHtml[] = R"(<!doctype html><html><head><meta charset="utf-8"><style>
body{display:flex;align-items:center;justify-content:center;height:100vh;margin:0;
background:#101418;color:#c9d4de;font:15px/1.8 system-ui,sans-serif}
.b{max-width:640px;padding:0 32px}
h1{font-size:18px;color:#e8eef4}code{background:#1c242c;padding:2px 6px;border-radius:4px}
</style></head><body><div class="b">
<h1>服务未能按时启动</h1>
<p>如果已有一个 light-agent 窗口在运行，请先关闭它（端口 18300
只能被一个实例占用），然后重新打开本程序。</p>
<p>详细原因见安装目录下的 <code>light-agent-server.log</code>。</p>
</div></body></html>)";

/* data: URL helper — percent-encode the UTF-8 payload so no character in the
 * inline pages can break URL parsing. */
static std::string data_url(const char *html) {
    static const char *hex = "0123456789ABCDEF";
    std::string out = "data:text/html;charset=utf-8,";
    for (const unsigned char *p = (const unsigned char *)html; *p; p++) {
        unsigned char c = *p;
        if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') ||
            c == '-' || c == '_' || c == '.' || c == '~') {
            out += (char)c;
        } else {
            out += '%';
            out += hex[c >> 4];
            out += hex[c & 15];
        }
    }
    return out;
}

static std::string startup_file_url(const std::wstring &path) {
    wchar_t absolute[4096], url[8192];
    DWORD size = GetFullPathNameW(path.c_str(), 4096, absolute, nullptr);
    if (!size || size >= 4096 || GetFileAttributesW(absolute) == INVALID_FILE_ATTRIBUTES)
        return {};
    DWORD length = 8192;
    if (FAILED(UrlCreateFromPathW(absolute, url, &length, 0))) return {};
    int bytes = WideCharToMultiByte(CP_UTF8, 0, url, (int)length, nullptr, 0, nullptr, nullptr);
    std::string result(bytes, '\0');
    if (bytes) WideCharToMultiByte(CP_UTF8, 0, url, (int)length, result.data(), bytes, nullptr, nullptr);
    // The URL is inserted into a double-quoted HTML attribute.
    std::string escaped;
    for (char c : result) {
        if (c == '&') escaped += "&amp;";
        else if (c == '"') escaped += "&quot;";
        else escaped += c;
    }
    return escaped;
}

static void navigate_main() {
    if (g_web) g_web->Navigate(g_url_main.c_str());
}

static void navigate_error() {
    if (g_web) g_web->Navigate(g_url_error.c_str());
}

/* Spawn the hidden backend server, output redirected to the log file.
 * Returns process handle (caller closes) or NULL on failure. */
static HANDLE spawn_server(const wchar_t *exe, const wchar_t *dir) {
    std::wstring log = std::wstring(dir) + L"\\light-agent-server.log";

    SECURITY_ATTRIBUTES sa;
    sa.nLength = sizeof(sa);
    sa.lpSecurityDescriptor = nullptr;
    sa.bInheritHandle = TRUE;

    /* Preserve the previous backend's last messages across a genuine crash. */
    HANDLE logf = CreateFileW(log.c_str(), FILE_APPEND_DATA,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, &sa,
                              OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);

    STARTUPINFOW si;
    PROCESS_INFORMATION pi;
    std::memset(&si, 0, sizeof(si));
    std::memset(&pi, 0, sizeof(pi));
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdOutput = logf;
    si.hStdError = logf;

    std::wstring cmdline = L"\"" + std::wstring(exe) + L"\" serve " +
                           std::to_wstring(kPort);
    wchar_t *cmd = new wchar_t[cmdline.size() + 1];
    wcscpy(cmd, cmdline.c_str());

    BOOL ok = CreateProcessW(exe, cmd, nullptr, nullptr, TRUE,
                             CREATE_NO_WINDOW, nullptr, dir, &si, &pi);
    delete[] cmd;
    if (logf) CloseHandle(logf);
    if (!ok)
        return nullptr;
    CloseHandle(pi.hThread);
    return pi.hProcess;
}

/* TCP reachability is not process liveness. A full listen queue or a busy
 * accept thread may reject a probe while an Agent task is still running. */
static bool backend_restart_required(DWORD process_wait) {
    return process_wait == WAIT_OBJECT_0;
}

/* Worker thread: wait for the backend; if it dies while booting (it has a
 * known intermittent crash), restart it a few times before giving up. Once
 * up, keep watching — a crash mid-session is recovered the same way (server
 * respawn + page reload) instead of leaving a dead white window. Navigation
 * is marshalled to the UI thread via PostMessage(WM_APP_NAV). */
static DWORD WINAPI boot_waiter(LPVOID) {
    int restarts = 0;
    bool ever_up = false;

    for (;;) {
        bool up = wait_server(kPort, 60000);
        if (up) {
            if (!ever_up)
                PostMessageA(g_hwnd, WM_APP_NAV, 1, 0);
            ever_up = true;
        } else {
            if (g_server && !backend_restart_required(WaitForSingleObject(g_server, 0))) {
                OutputDebugStringA("light-agent: startup HTTP probe timed out; backend is alive, preserving running tasks.\n");
                continue;
            }
            if (restarts >= 3) {
                PostMessageA(g_hwnd, WM_APP_NAV, 2, 0);
                return 0;
            }
            restarts++;
            if (g_server) CloseHandle(g_server);
            std::wstring dir = exe_dir();
            std::wstring exe = dir + L"\\light-agent.exe";
            g_server = spawn_server(exe.c_str(), dir.c_str());
            if (!g_server)
                return 0;
            continue;
        }

        /* Steady state: watch the server process; recover from crashes. */
        ULONGLONG healthy_since = GetTickCount64();
        int probes = 0;
        unsigned failed_probes = 0;
        for (;;) {
            DWORD wait = WaitForSingleObject(g_server, 2000);
            if (backend_restart_required(wait)) {
                /* server process exited (crash or clean stop). If the window
                 * is gone too, exit; otherwise restart the backend. */
                if (!IsWindow(g_hwnd) || restarts >= 3)
                    return 0; /* window close terminates us anyway */
                restarts++;
                CloseHandle(g_server);
                std::wstring dir = exe_dir();
                std::wstring exe = dir + L"\\light-agent.exe";
                g_server = spawn_server(exe.c_str(), dir.c_str());
                if (!g_server)
                    return 0;
                break; /* re-enter wait_server loop, then reload the page */
            }
            /* Probe failure alone cannot authorize killing a live backend. */
            if (++probes % 3 == 0) {
                if (wait_server(kPort, 1000)) {
                    failed_probes = 0;
                    continue;
                }
                if (++failed_probes == 10)
                    OutputDebugStringA("light-agent: backend still alive but HTTP probe failed repeatedly; preserving running tasks.\n");
            }
            /* Five stable minutes restore the full crash budget. */
            if (GetTickCount64() - healthy_since > 300000)
                restarts = 0;
        }
    }
}

/* Real button controls retain keyboard, capture and accessibility behavior.
 * Paint the symbols with lines so they do not depend on installed glyph fonts. */
static LRESULT CALLBACK caption_button_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_MOUSEMOVE && g_hover_button != hwnd) {
        g_hover_button = hwnd;
        TRACKMOUSEEVENT track = { sizeof(track), TME_LEAVE, hwnd, 0 };
        TrackMouseEvent(&track);
        InvalidateRect(hwnd, nullptr, FALSE);
    } else if (msg == WM_MOUSELEAVE) {
        if (g_hover_button == hwnd) g_hover_button = nullptr;
        InvalidateRect(hwnd, nullptr, FALSE);
    }
    return CallWindowProcW(g_button_proc, hwnd, msg, wp, lp);
}

static void layout_chrome(HWND hwnd) {
    RECT rc;
    GetClientRect(hwnd, &rc);
    for (int i = 0; i < 3; ++i)
        if (g_caption_buttons[i])
            MoveWindow(g_caption_buttons[i], rc.right - scaled(46) * (3 - i),
                       0, scaled(46), chrome_height(), TRUE);
    if (g_caption_buttons[1])
        SetWindowTextW(g_caption_buttons[1], IsZoomed(hwnd) ? L"还原" : L"最大化");
    if (g_ctrl && !IsIconic(hwnd)) {
        rc.top = chrome_height();
        g_ctrl->put_Bounds(rc);
    }
    RECT bar = {0, 0, rc.right, chrome_height()};
    InvalidateRect(hwnd, &bar, FALSE);
}

static void draw_caption_button(HWND hwnd, const DRAWITEMSTRUCT *item) {
    const int id = (int)item->CtlID - 100;
    const bool highlighted = item->hwndItem == g_hover_button || (item->itemState & ODS_SELECTED);
    const COLORREF background = highlighted ? (id == 2 ? RGB(196, 43, 28) : RGB(226, 232, 240))
                                            : RGB(248, 250, 252);
    HBRUSH brush = CreateSolidBrush(background);
    FillRect(item->hDC, &item->rcItem, brush);
    DeleteObject(brush);
    int cx = (item->rcItem.left + item->rcItem.right) / 2;
    int cy = (item->rcItem.top + item->rcItem.bottom) / 2;
    int half = scaled(5);
    HPEN pen = CreatePen(PS_SOLID, scaled(1), highlighted && id == 2 ? RGB(255,255,255) : RGB(51,65,85));
    HGDIOBJ oldpen = SelectObject(item->hDC, pen);
    HGDIOBJ oldbrush = SelectObject(item->hDC, GetStockObject(NULL_BRUSH));
    if (id == 0) {
        MoveToEx(item->hDC, cx-half, cy, nullptr); LineTo(item->hDC, cx+half+1, cy);
    } else if (id == 1) {
        if (IsZoomed(hwnd)) {
            int offset = scaled(3);
            Rectangle(item->hDC, cx-half+offset, cy-half, cx+half+1, cy+half-offset+1);
            HBRUSH fill = CreateSolidBrush(background);
            RECT front = {cx-half, cy-half+offset, cx+half-offset+1, cy+half+1};
            FillRect(item->hDC, &front, fill); DeleteObject(fill);
            Rectangle(item->hDC, front.left, front.top, front.right, front.bottom);
        } else Rectangle(item->hDC, cx-half, cy-half, cx+half+1, cy+half+1);
    } else {
        MoveToEx(item->hDC, cx-half, cy-half, nullptr); LineTo(item->hDC, cx+half+1, cy+half+1);
        MoveToEx(item->hDC, cx+half, cy-half, nullptr); LineTo(item->hDC, cx-half-1, cy+half+1);
    }
    SelectObject(item->hDC, oldpen); SelectObject(item->hDC, oldbrush); DeleteObject(pen);
    if (item->itemState & ODS_FOCUS) {
        RECT focus = item->rcItem; InflateRect(&focus, -scaled(5), -scaled(5));
        DrawFocusRect(item->hDC, &focus);
    }
}

static LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
    switch (msg) {
    case WM_NCPAINT:
        return 0; /* Custom chrome paints the full client area, including edges. */
    case WM_NCACTIVATE:
        return TRUE; /* Prevent the default thick-frame activation border. */
    case WM_ERASEBKGND: {
        RECT client;GetClientRect(hwnd,&client);
        HBRUSH brush=CreateSolidBrush(RGB(248,250,252));
        FillRect((HDC)wp,&client,brush);DeleteObject(brush);
        return 1;
    }
    case WM_NCCALCSIZE:
        /* Keep WS_THICKFRAME for native resize/snap behavior, but make the
         * custom chrome and WebView occupy the whole window. DefWindowProc
         * otherwise reserves a system frame that shows as black side strips
         * when our maximized outer rectangle is aligned to the work area. */
        return 0;
    case WM_CREATE: {
        g_dpi = GetDpiForWindow(hwnd);
        const wchar_t *names[] = {L"最小化", L"最大化", L"关闭"};
        for (int i = 0; i < 3; ++i) {
            g_caption_buttons[i] = CreateWindowW(L"BUTTON", names[i],
                WS_CHILD | WS_VISIBLE | WS_TABSTOP | BS_OWNERDRAW,
                0, 0, 0, 0, hwnd, (HMENU)(INT_PTR)(100+i), GetModuleHandleW(nullptr), nullptr);
            WNDPROC original = (WNDPROC)SetWindowLongPtrW(g_caption_buttons[i], GWLP_WNDPROC,
                                                        (LONG_PTR)caption_button_proc);
            if (!g_button_proc) g_button_proc = original;
        }
        layout_chrome(hwnd);
        return 0;
    }
    case WM_COMMAND:
        if (HIWORD(wp) == BN_CLICKED && LOWORD(wp) >= 100 && LOWORD(wp) <= 102) {
            if (LOWORD(wp) == 102) PostMessageW(hwnd, WM_CLOSE, 0, 0);
            else ShowWindow(hwnd, LOWORD(wp) == 100 ? SW_MINIMIZE :
                            (IsZoomed(hwnd) ? SW_RESTORE : SW_MAXIMIZE));
            return 0;
        }
        break;
    case WM_DRAWITEM:
        if (wp >= 100 && wp <= 102) {
            draw_caption_button(hwnd, (const DRAWITEMSTRUCT *)lp);
            return TRUE;
        }
        break;
    case WM_DPICHANGED: {
        g_dpi = HIWORD(wp);
        const RECT *rc = (const RECT *)lp;
        SetWindowPos(hwnd, nullptr, rc->left, rc->top, rc->right-rc->left, rc->bottom-rc->top,
                     SWP_NOZORDER | SWP_NOACTIVATE);
        layout_chrome(hwnd);
        return 0;
    }
    case WM_GETMINMAXINFO: {
        MINMAXINFO *info = (MINMAXINFO *)lp;
        info->ptMinTrackSize = {scaled(640), scaled(480)};
        MONITORINFO monitor = {sizeof(monitor)};
        if (GetMonitorInfoW(MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST), &monitor)) {
            info->ptMaxPosition = {monitor.rcWork.left-monitor.rcMonitor.left,
                                   monitor.rcWork.top-monitor.rcMonitor.top};
            info->ptMaxSize = {monitor.rcWork.right-monitor.rcWork.left,
                               monitor.rcWork.bottom-monitor.rcWork.top};
        }
        return 0;
    }
    case WM_NCHITTEST: {
        /* The desktop shell is frameless. Only the native window controls
         * consume clicks; the rest of this strip is a drag target. */
        POINT pt = { GET_X_LPARAM(lp), GET_Y_LPARAM(lp) };
        RECT wr;
        GetWindowRect(hwnd, &wr);
        const int edge = scaled(6);
        const bool left = pt.x < wr.left + edge, right = pt.x >= wr.right - edge;
        const bool top = pt.y < wr.top + edge, bottom = pt.y >= wr.bottom - edge;
        if (!IsZoomed(hwnd)) {
            if (top && left) return HTTOPLEFT;
            if (top && right) return HTTOPRIGHT;
            if (bottom && left) return HTBOTTOMLEFT;
            if (bottom && right) return HTBOTTOMRIGHT;
            if (left) return HTLEFT;
            if (right) return HTRIGHT;
            if (top) return HTTOP;
            if (bottom) return HTBOTTOM;
        }
        ScreenToClient(hwnd, &pt);
        if (pt.y >= 0 && pt.y < chrome_height()) {
            RECT cr;
            GetClientRect(hwnd, &cr);
            return pt.x >= cr.right - scaled(138) ? HTCLIENT : HTCAPTION;
        }
        return HTCLIENT;
    }
    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC dc = BeginPaint(hwnd, &ps);
        RECT cr;
        GetClientRect(hwnd, &cr);
        HBRUSH bg = CreateSolidBrush(RGB(248, 250, 252));
        RECT bar = { 0, 0, cr.right, chrome_height() };
        FillRect(dc, &bar, bg);
        DeleteObject(bg);
        SetBkMode(dc, TRANSPARENT);
        SetTextColor(dc, RGB(103, 100, 96));
        HFONT font = CreateFontW(-scaled(13), 0, 0, 0, FW_MEDIUM, FALSE, FALSE, FALSE,
            DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY,
            DEFAULT_PITCH, L"Segoe UI");
        HGDIOBJ old = SelectObject(dc, font);
        if (g_icon_small) DrawIconEx(dc, scaled(14), scaled(10), g_icon_small,
                                     scaled(20), scaled(20), 0, nullptr, DI_NORMAL);
        RECT title = { scaled(g_icon_small ? 44 : 16), 0, cr.right-scaled(154), chrome_height() };
        DrawTextW(dc, L"light-agent", -1, &title, DT_SINGLELINE | DT_VCENTER | DT_LEFT | DT_NOPREFIX | DT_END_ELLIPSIS);
        SelectObject(dc, old);
        DeleteObject(font);
        EndPaint(hwnd, &ps);
        return 0;
    }
    case WM_APP_NAV:
        if (wp == 1 && g_startup_animation && g_web) {
            g_web->ExecuteScript(L"window.startupPhase && startupPhase(3)", nullptr);
            if (!SetTimer(hwnd, kStartupFinishTimer, g_startup_battle ? 3200 : 720, nullptr))
                navigate_main();
        } else if (wp == 1)
            navigate_main();
        else if (wp == 2) {
            KillTimer(hwnd, kStartupFinishTimer);
            navigate_error();
        }
        return 0;
    case WM_TIMER:
        if (wp == kStartupFinishTimer) {
            KillTimer(hwnd, kStartupFinishTimer);
            navigate_main();
            return 0;
        }
        break;
    case WM_SIZE: {
        if (wp != SIZE_MINIMIZED) layout_chrome(hwnd);
        return 0;
    }
    case WM_DESTROY:
        if (g_task_pet && IsWindow(g_task_pet)) DestroyWindow(g_task_pet);
        g_task_pet = nullptr;
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcA(hwnd, msg, wp, lp);
}

int WINAPI WinMain(HINSTANCE, HINSTANCE, LPSTR, int) {
    SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
    std::wstring dir = exe_dir();
    auto dragon_asset = [&dir](const wchar_t *name) {
        const auto installed = dir + L"\\" + name;
        return GetFileAttributesW(installed.c_str()) != INVALID_FILE_ATTRIBUTES
            ? installed : dir + L"\\..\\assets\\" + name;
    };
    g_dragon_gif = dragon_asset(L"dragon-girl.gif");
    g_dragon_sheet = dragon_asset(L"dragon-girl-pet.png");
    g_dragon_mount = dragon_asset(L"dragon-girl-mounted.png");
    g_task_pet_asset = dir + L"\\light-agent-pet.png";
    if (GetFileAttributesW(g_task_pet_asset.c_str()) == INVALID_FILE_ATTRIBUTES)
        g_task_pet_asset = dir + L"\\..\\assets\\light-agent-pet.png";
    std::wstring exe = dir + L"\\light-agent.exe";

    /* Work around two Edge/WebView2 behaviors that stall navigations on
     * networks where Microsoft's backends are slow or unreachable:
     *  - SmartScreen / device-bound-session checks pause every main-frame
     *    load at URL_REQUEST_DELEGATE_CONNECTED (observed in netlog) and the
     *    pause never resumes when the check cannot complete;
     *  - Local Network Access gating similarly parks requests to localhost.
     * Preserve any user-provided value of the environment variable. */
    {
        wchar_t existing[1024];
        DWORD n = GetEnvironmentVariableW(L"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS",
                                          existing, 1024);
        std::wstring args = L"--disable-features=msSmartScreenProtection,"
                            L"SmartScreen,DeviceBoundSessions,LocalNetworkAccess";
        if (n > 0 && n < 1024)
            args = std::wstring(existing) + L" " + args;
        SetEnvironmentVariableW(L"WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", args.c_str());
    }

    /* Show the window immediately with the inline starting page — never a
     * dead silent desktop. The worker thread switches over to the console
     * once the backend accepts connections (or renders the error page). */
    WNDCLASSA wc;
    std::memset(&wc, 0, sizeof(wc));
    wc.lpfnWndProc = wnd_proc;
    wc.hInstance = GetModuleHandleA(nullptr);
    wc.hCursor = LoadCursorA(nullptr, MAKEINTRESOURCEA(32512));
    /* Keep the native title/taskbar mark aligned with the web console and
     * installer shortcut. The ICO is installed next to the desktop shell. */
    std::wstring icon = dir + L"\\light-agent-logo.ico";
    if(GetFileAttributesW(icon.c_str())==INVALID_FILE_ATTRIBUTES)icon=dir+L"\\light-agent.ico";
    g_icon_big = (HICON)LoadImageW(nullptr, icon.c_str(), IMAGE_ICON, 256, 256,
                                   LR_LOADFROMFILE);
    g_icon_small = (HICON)LoadImageW(nullptr, icon.c_str(), IMAGE_ICON, 32, 32,
                                     LR_LOADFROMFILE);
    wc.hIcon = g_icon_big ? g_icon_big : g_icon_small;
    wc.lpszClassName = "light-agent";
    RegisterClassA(&wc);
    g_hwnd = CreateWindowA("light-agent", "light-agent",
                           WS_POPUP | WS_THICKFRAME | WS_CLIPCHILDREN | WS_MINIMIZEBOX |
                           WS_MAXIMIZEBOX | WS_SYSMENU,
                           CW_USEDEFAULT, CW_USEDEFAULT,
                           1280, 800, nullptr, nullptr, wc.hInstance, nullptr);
    if (!g_hwnd) {
        MessageBoxW(nullptr, L"Failed to create the main window.",
                    L"light-agent", MB_ICONERROR);
        return 1;
    }
    /* Windows 11 adds a DWM border even to a custom thick-frame window.
     * Disable that border while retaining resize/snap hit testing. */
    HMODULE dwm=LoadLibraryW(L"dwmapi.dll");
    if(dwm){
        typedef HRESULT (WINAPI *SetAttribute)(HWND,DWORD,LPCVOID,DWORD);
        auto setAttribute=(SetAttribute)GetProcAddress(dwm,"DwmSetWindowAttribute");
        const COLORREF noBorder=0xfffffffe;
        if(setAttribute)setAttribute(g_hwnd,34,&noBorder,sizeof(noBorder));
        FreeLibrary(dwm);
    }
    /* WNDCLASS only supplies a fallback icon. Set both window slots
     * explicitly so the installed app, taskbar and Alt+Tab use the same
     * generated brain ICO as setup and its shortcuts. */
    if (g_icon_big)
        SendMessageW(g_hwnd, WM_SETICON, ICON_BIG, (LPARAM)g_icon_big);
    if (g_icon_small)
        SendMessageW(g_hwnd, WM_SETICON, ICON_SMALL, (LPARAM)g_icon_small);
    ShowWindow(g_hwnd, SW_SHOW);
    UpdateWindow(g_hwnd);

    /* The desktop shell has its own compact window chrome, so do not
     * duplicate the web console header inside the content viewport. */
    g_url_main = L"http://localhost:" + std::to_wstring(kPort) + L"/?desktop=1";
    {
        std::string err8 = data_url(kErrorHtml);
        g_url_error = std::wstring(err8.begin(), err8.end());
    }

    /* Backend first (so its boot overlaps the WebView2 env creation), then
     * the raw COM host — the sequence proven by the wv2test diagnostic tool:
     * window shown → COM STA → env (pump) → controller (pump) → navigate. */
    g_server = spawn_server(exe.c_str(), dir.c_str());
    if (!g_server) {
        MessageBoxW(nullptr, L"Failed to start the light-agent server.",
                    L"light-agent", MB_ICONERROR);
        return 1;
    }

    HRESULT hr = CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);
    if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) {
        MessageBoxW(nullptr, L"COM initialization failed.",
                    L"light-agent", MB_ICONERROR);
        return 1;
    }

    if (!load_runtime()) {
        MessageBoxW(nullptr,
                    L"未找到 WebView2 运行时。\n请安装 Microsoft Edge WebView2 Runtime 后重试。",
                    L"light-agent", MB_ICONERROR);
        return 1;
    }

    DisableFeaturesOptions opts(
        L"--disable-features=msSmartScreenProtection,"
        L"SmartScreen,DeviceBoundSessions,LocalNetworkAccess");
    /* UDF: %APPDATA%\<exe name>, same location the previous builds used */
    wchar_t udf[MAX_PATH];
    {
        char appdata[MAX_PATH];
        DWORD n = GetEnvironmentVariableA("APPDATA", appdata, MAX_PATH);
        if (n > 0 && n < MAX_PATH) {
            std::string u = std::string(appdata) + "\\cognitive-os-agent-desktop.exe";
            MultiByteToWideChar(CP_UTF8, 0, u.c_str(), -1, udf, MAX_PATH);
        } else {
            wcscpy(udf, dir.c_str());
        }
    }

    EnvHandler *eh = new EnvHandler();
    hr = g_create_env(true, 0 /*installed*/, udf, &opts, eh);
    if (FAILED(hr) || !pump_until(&eh->done, 60000) || FAILED(eh->hr) || !eh->env) {
        MessageBoxW(nullptr, L"WebView2 environment creation failed.",
                    L"light-agent", MB_ICONERROR);
        return 1;
    }

    CtrlHandler *ch = new CtrlHandler();
    hr = eh->env->CreateCoreWebView2Controller(g_hwnd, ch);
    if (FAILED(hr) || !pump_until(&ch->done, 60000) || FAILED(ch->hr) || !ch->ctrl) {
        MessageBoxW(nullptr, L"WebView2 controller creation failed.",
                    L"light-agent", MB_ICONERROR);
        return 1;
    }

    g_ctrl = ch->ctrl;
    g_ctrl->AddRef();
    HRESULT hrg = ch->ctrl->get_CoreWebView2(&g_web);
    if (FAILED(hrg) || !g_web) {
        MessageBoxW(nullptr, L"WebView2 object unavailable.",
                    L"light-agent", MB_ICONERROR);
        return 1;
    }

    /* Repo/provider links open in the default browser; local console
     * navigation (including the boot data: pages) proceeds in-frame. */
    {
        NavStartHandler *nav = new NavStartHandler();
        NewWindowHandler *win = new NewWindowHandler();
        EventRegistrationToken token_nav, token_win;
        g_web->add_NavigationStarting(nav, &token_nav);
        g_web->add_NewWindowRequested(win, &token_win);
        auto *pet = new TaskPetHandler();
        EventRegistrationToken token_pet;
        g_web->add_WebMessageReceived(pet, &token_pet);
        pet->Release();
    }

    RECT rc;
    GetClientRect(g_hwnd, &rc);
    rc.top += chrome_height();
    g_ctrl->put_Bounds(rc);

    std::wstring startup_path;
    /* Starting page first, then the boot watcher takes over. */
    {
        std::string starting=kStartingHtml;
        bool animate=true;
        wchar_t local[MAX_PATH];
        if(GetEnvironmentVariableW(L"LOCALAPPDATA",local,MAX_PATH)>0){
            std::wstring setting=std::wstring(local)+L"\\light-agent\\desktop-startup-animation";
            if(GetFileAttributesW(setting.c_str())==INVALID_FILE_ATTRIBUTES)
                setting=std::wstring(local)+L"\\cognitive-os-agent\\desktop-startup-animation";
            FILE *file=_wfopen(setting.c_str(),L"rb");
            if(file){animate=fgetc(file)!='0';fclose(file);}
            std::wstring mediaPath=std::wstring(local)+L"\\light-agent\\desktop-startup-media";
            FILE *mediaFile=animate?_wfopen(mediaPath.c_str(),L"rb"):nullptr;
            std::string media;
            if(mediaFile){
                char chunk[4096];size_t count;
                while((count=fread(chunk,1,sizeof chunk,mediaFile))>0 && media.size()+count<=4*1024*1024)media.append(chunk,count);
                fclose(mediaFile);
            }
            g_startup_animation = animate;
            if (!animate) starting.insert(starting.find("</head>"),
                "<style>*,*::before,*::after{animation:none!important;transition:none!important}</style>");
            const bool use_default = animate && media.empty();
            if (use_default) media = startup_file_url(g_dragon_gif);
            if (!media.empty()) {
                bool video=media.rfind("data:video/mp4;base64,",0)==0||media.rfind("data:video/webm;base64,",0)==0;
                bool image=media.rfind("data:image/gif;base64,",0)==0||media.rfind("data:image/webp;base64,",0)==0;
                if(use_default || ((video||image) && media.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",media.find(',')+1)==std::string::npos)){
                    size_t body=starting.find("</body>");
                    std::string tag=video?"<video autoplay muted loop playsinline src=\"":"<img alt=\"\" src=\"";
                    tag+=media+"\" class=\"startup-media\">";
                    if(video)tag+="</video>";
                    if (use_default) {
                        const auto hero = startup_file_url(g_dragon_sheet);
                        const auto battle = startup_file_url(g_dragon_mount);
                        if (!hero.empty() && !battle.empty()) {
                            tag += R"SVG(<svg class="startup-dragon" viewBox="0 0 230 95" aria-hidden="true">
<path class="fire" fill="#ff5928" d="M149 53 Q187 36 217 46L203 52 229 57 205 61 214 69Q176 67 149 57Z"/>
<path class="fire" fill="#ffdd88" d="M150 54Q181 49 217 56Q183 62 150 56Z"/>
<path fill="#5d1026" stroke="#eb5670" stroke-width="2" d="M17 66Q52 78 68 58L45 20 83 34 98 12 115 39 128 39 140 25 141 42 157 47 150 56 132 58Q114 79 93 69L75 83 78 68Q45 87 17 66Z"/>
<circle fill="#ffc971" cx="143" cy="47" r="2"/></svg>)SVG";
                            tag += "<img alt=\"\" class=\"startup-hero\" src=\"" + hero + "\">";
                            tag += "<img alt=\"\" class=\"battle-shot\" src=\"" + battle + "\">";
                            g_startup_battle = true;
                        }
                    }
                    size_t status=starting.find("<div class=\"aura\"");
                    if(body!=std::string::npos && status!=std::string::npos)starting.insert(status,tag);
                }
            }
        }
        HRESULT navigation = E_FAIL;
        wchar_t temporary[MAX_PATH], page[MAX_PATH];
        DWORD temporary_length = GetTempPathW(MAX_PATH, temporary);
        if (temporary_length && temporary_length < MAX_PATH &&
            GetTempFileNameW(temporary, L"las", 0, page)) {
            startup_path = page;
            startup_path += L".html";
            if (MoveFileW(page, startup_path.c_str())) {
                if (FILE *file = _wfopen(startup_path.c_str(), L"wb")) {
                    const bool written = fwrite(starting.data(), 1, starting.size(), file) == starting.size();
                    if (fclose(file) == 0 && written) {
                        WCHAR url[4096];
                        DWORD length = 4096;
                        if (SUCCEEDED(UrlCreateFromPathW(startup_path.c_str(), url, &length, 0))) {
                            g_url_startup = url;
                            navigation = g_web->Navigate(url);
                        }
                    }
                }
            } else {
                DeleteFileW(page);
                startup_path.clear();
            }
        }
        if (FAILED(navigation)) {
            g_startup_battle = false;
            const auto fallback = data_url(kStartingHtml);
            const std::wstring url(fallback.begin(), fallback.end());
            g_web->Navigate(url.c_str());
        }
    }

    HANDLE boot_thread = CreateThread(nullptr, 0, boot_waiter, nullptr, 0, nullptr);
    if (boot_thread)
        CloseHandle(boot_thread); /* joined implicitly by process exit */

    /* UI message loop */
    MSG msg;
    while (GetMessageA(&msg, nullptr, 0, 0) > 0) {
        if ((msg.hwnd == g_hwnd && msg.message == WM_KEYDOWN && msg.wParam == VK_TAB) ||
            (GetFocus() == g_caption_buttons[0] || GetFocus() == g_caption_buttons[1] ||
             GetFocus() == g_caption_buttons[2])) {
            if (IsDialogMessageW(g_hwnd, &msg)) continue;
        }
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }

    if (g_server) {
        TerminateProcess(g_server, 0);
        CloseHandle(g_server);
    }
    if (g_icon_big)
        DestroyIcon(g_icon_big);
    if (g_icon_small && g_icon_small != g_icon_big)
        DestroyIcon(g_icon_small);
    if (!startup_path.empty()) DeleteFileW(startup_path.c_str());
    CoUninitialize();
    return 0;
}

