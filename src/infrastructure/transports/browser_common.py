# src/infrastructure/transports/browser_common.py
"""Shared pieces of the browser-based Ozon transports.

Extracted from ``browser_json.py`` so every transport (and the
tests) can use them without importing the whole
BrowserJsonTransport module:

- the lazy invisible-playwright factory (GPU-safe);
- the retryable-errors tuple (RuntimeError / TimeoutError /
  Playwright Error when installed);
- the stealth init script that patches the common Cloudflare
  headless-detection signals;
- the extension-pattern resource blocker (images/fonts/media).
"""
from __future__ import annotations

from typing import Any


def import_invisible_playwright() -> type:
    """Lazy import of invisible-playwright, wrapped with
    GPU-safe software-rendering prefs (see gpu_safety.py).

    The library is heavy (patched Playwright + browser binaries)
    and absent in some environments that import this module;
    transports call this inside the async generators that
    actually need a browser.
    """
    from invisible_playwright.async_api import (
        InvisiblePlaywright,
    )

    from infrastructure.transports.gpu_safety import (
        make_gpu_safe,
    )
    return make_gpu_safe(InvisiblePlaywright)

def _get_retryable_errors() -> tuple[type[BaseException], ...]:
    """Return the tuple of exception types that should trigger a retry.

    Built dynamically so we can include ``invisible_playwright``'s
    ``Error`` class only when the library is installed. Always
    includes ``RuntimeError`` and the standard ``TimeoutError`` /
    ``asyncio.TimeoutError`` (in Python 3.11+ these are unified, but
    we keep both for safety on 3.10).
    """
    import asyncio as _asyncio

    types: list[type[BaseException]] = [
        RuntimeError,
        TimeoutError,
        _asyncio.TimeoutError,
    ]
    try:
        from invisible_playwright._pw._impl._errors import (
            Error as PlaywrightError,
        )
        types.append(PlaywrightError)
    except ImportError:
        # invisible-playwright not installed — that's OK, the retry
        # still works on RuntimeError and TimeoutError.
        pass

    return tuple(types)


# Module-level cache so we don't re-import on every retry.
_RETRYABLE_ERRORS: tuple[type[BaseException], ...] | None = None


def _retryable_errors() -> tuple[type[BaseException], ...]:
    global _RETRYABLE_ERRORS
    if _RETRYABLE_ERRORS is None:
        _RETRYABLE_ERRORS = _get_retryable_errors()
    return _RETRYABLE_ERRORS

# Stealth init script — patches the most common signals Cloudflare
# and Yandex SmartCaptcha use to detect automated / headless browsers.
# Adapted from playwright-stealth (https://github.com/
# Mattwmaster58/playwright_stealth) and enhanced with anti-fingerprinting
# techniques specifically targeting Yandex's bot detection.
#
# Applied to every fresh page via ``page.add_init_script`` so the
# patches run before any page JS executes.
_STEALTH_INIT_SCRIPT = """
// === Core WebDriver hiding ===
Object.defineProperty(navigator, 'webdriver', {
    get: () => undefined,
    configurable: true,
});

// Delete webdriver from prototype chain (deeper hiding)
try {
    delete Object.getPrototypeOf(navigator).webdriver;
} catch (e) {}

// === Chrome runtime object ===
if (!window.chrome) {
    window.chrome = {
        runtime: {},
        app: {},
        csi: () => {},
        loadTimes: () => {},
    };
}

// === Notification.permission ===
if (window.Notification) {
    Object.defineProperty(Notification, 'permission', {
        get: () => 'default',
        configurable: true,
    });
}

// === Plugins (real browsers have PDF viewer) ===
Object.defineProperty(navigator, 'plugins', {
    get: () => [
        {
            name: 'PDF Viewer',
            filename: 'internal-pdf-viewer',
            description: 'Portable Document Format',
            length: 1,
            0: {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'},
        },
        {
            name: 'Chrome PDF Viewer',
            filename: 'internal-pdf-viewer',
            description: 'Portable Document Format',
            length: 1,
            0: {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'},
        },
        {
            name: 'Chromium PDF Viewer',
            filename: 'internal-pdf-viewer',
            description: 'Portable Document Format',
            length: 1,
            0: {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'},
        },
    ],
    configurable: true,
});

// === MimeTypes ===
Object.defineProperty(navigator, 'mimeTypes', {
    get: () => [
        {
            type: 'application/pdf',
            suffixes: 'pdf',
            description: 'Portable Document Format',
            enabledPlugin: {
                name: 'PDF Viewer',
                filename: 'internal-pdf-viewer',
                description: 'Portable Document Format',
            },
        },
        {
            type: 'text/pdf',
            suffixes: 'pdf',
            description: 'Portable Document Format',
            enabledPlugin: {
                name: 'PDF Viewer',
                filename: 'internal-pdf-viewer',
                description: 'Portable Document Format',
            },
        },
    ],
    configurable: true,
});

// === Languages (realistic Russian user) ===
Object.defineProperty(navigator, 'languages', {
    get: () => ['ru-RU', 'ru', 'en-US', 'en'],
    configurable: true,
});

Object.defineProperty(navigator, 'language', {
    get: () => 'ru-RU',
    configurable: true,
});

// === Permissions API ===
const originalQuery = window.navigator.permissions
    ? window.navigator.permissions.query
    : null;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({state: 'default', onchange: null})
            : originalQuery(parameters)
    );
}

// === Window dimensions (headless browsers report 0) ===
if (window.outerWidth === 0 || window.outerHeight === 0) {
    Object.defineProperty(window, 'outerWidth', {
        get: () => window.innerWidth || 1280,
        configurable: true,
    });
    Object.defineProperty(window, 'outerHeight', {
        get: () => window.innerHeight + 85 || 720,
        configurable: true,
    });
}

// === Screen properties (consistent with viewport) ===
// Yandex checks screen.colorDepth, screen.pixelDepth
if (window.screen) {
    Object.defineProperty(screen, 'colorDepth', {
        get: () => 24,
    });
    Object.defineProperty(screen, 'pixelDepth', {
        get: () => 24,
    });
}

// === navigator.hardwareConcurrency (real CPU cores) ===
Object.defineProperty(navigator, 'hardwareConcurrency', {
    get: () => 8,
    configurable: true,
});

// === navigator.deviceMemory (real RAM) ===
Object.defineProperty(navigator, 'deviceMemory', {
    get: () => 8,
    configurable: true,
});

// === navigator.maxTouchPoints (desktop = 0) ===
Object.defineProperty(navigator, 'maxTouchPoints', {
    get: () => 0,
    configurable: true,
});

// === navigator.platform (consistent OS) ===
Object.defineProperty(navigator, 'platform', {
    get: () => 'Win32',
    configurable: true,
});

// === navigator.vendor (Chromium) ===
Object.defineProperty(navigator, 'vendor', {
    get: () => 'Google Inc.',
    configurable: true,
});

// === navigator.doNotTrack (privacy-conscious user) ===
Object.defineProperty(navigator, 'doNotTrack', {
    get: () => null,
    configurable: true,
});

// === Battery API (hide automation signal) ===
if (navigator.getBattery) {
    navigator.getBattery = () => Promise.resolve({
        charging: true,
        chargingTime: 0,
        dischargingTime: Infinity,
        level: 1,
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => true,
    });
}

// === Connection API (realistic network) ===
if (navigator.connection) {
    Object.defineProperty(navigator.connection, 'rtt', {
        get: () => 50,
        configurable: true,
    });
    Object.defineProperty(navigator.connection, 'downlink', {
        get: () => 10,
        configurable: true,
    });
    Object.defineProperty(navigator.connection, 'effectiveType', {
        get: () => '4g',
        configurable: true,
    });
    Object.defineProperty(navigator.connection, 'saveData', {
        get: () => false,
        configurable: true,
    });
}

// === Canvas fingerprinting defense ===
// Add subtle noise to canvas rendering to avoid consistent fingerprint
const canvasProto = HTMLCanvasElement.prototype;
const originalToDataURL = canvasProto.toDataURL;
const originalToBlob = canvasProto.toBlob;
const originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;

// Inject minimal noise (1-2 pixels) to break consistent fingerprints
const addCanvasNoise = (imageData) => {
    if (!imageData || !imageData.data) return;
    const data = imageData.data;
    // Change 1-2 random pixels slightly (barely visible)
    for (let i = 0; i < 2; i++) {
        const idx = Math.floor(Math.random() * (data.length / 4)) * 4;
        data[idx] = (data[idx] + Math.floor(Math.random() * 3) - 1) & 0xFF;
    }
};

CanvasRenderingContext2D.prototype.getImageData = function(...args) {
    const imageData = originalGetImageData.apply(this, args);
    addCanvasNoise(imageData);
    return imageData;
};

canvasProto.toDataURL = function(...args) {
    const ctx = this.getContext('2d');
    if (ctx) {
        const imageData = ctx.getImageData(0, 0, this.width, this.height);
        addCanvasNoise(imageData);
        ctx.putImageData(imageData, 0, 0);
    }
    return originalToDataURL.apply(this, args);
};

canvasProto.toBlob = function(...args) {
    const ctx = this.getContext('2d');
    if (ctx) {
        const imageData = ctx.getImageData(0, 0, this.width, this.height);
        addCanvasNoise(imageData);
        ctx.putImageData(imageData, 0, 0);
    }
    return originalToBlob.apply(this, args);
};

// === WebGL fingerprinting defense ===
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    // Mask UNMASKED_VENDOR_WEBGL and UNMASKED_RENDERER_WEBGL
    if (parameter === 37445) {
        return 'Intel Inc.';
    }
    if (parameter === 37446) {
        return 'Intel Iris OpenGL Engine';
    }
    return getParameter.apply(this, arguments);
};

// === AudioContext fingerprinting defense ===
const AudioContext = window.AudioContext || window.webkitAudioContext;
if (AudioContext) {
    const originalCreateOscillator = AudioContext.prototype.createOscillator;
    AudioContext.prototype.createOscillator = function() {
        const oscillator = originalCreateOscillator.apply(this, arguments);
        const originalStart = oscillator.start;
        oscillator.start = function(when) {
            // Add 0.0001s jitter to break audio fingerprinting
            const jitteredWhen = when ? when + (Math.random() * 0.0001) : when;
            return originalStart.call(this, jitteredWhen);
        };
        return oscillator;
    };
}

// === ClientRects fingerprinting (font metrics) ===
// Block consistent measurements by adding sub-pixel noise
const originalGetBoundingClientRect = Element.prototype.getBoundingClientRect;
Element.prototype.getBoundingClientRect = function() {
    const rect = originalGetBoundingClientRect.apply(this, arguments);
    // Add ±0.0001px noise to width/height (invisible, breaks fingerprint)
    return {
        ...rect,
        width: rect.width + (Math.random() * 0.0002 - 0.0001),
        height: rect.height + (Math.random() * 0.0002 - 0.0001),
        toJSON: rect.toJSON,
    };
};

// === document.title (hide JUGGLER session ID) ===
try {
    Object.defineProperty(document, 'title', {
        get: () => document.querySelector('title')?.textContent || '',
        set: (value) => {
            let titleEl = document.querySelector('title');
            if (!titleEl) {
                titleEl = document.createElement('title');
                document.head ? document.head.appendChild(titleEl) : null;
            }
            titleEl.textContent = value;
        },
        configurable: true,
    });
    if (!document.title || document.title.startsWith('JUGGLER')) {
        document.title = '';
    }
} catch (e) {
    try { document.title = ''; } catch (e2) {}
}

// === Date/Time consistency ===
// Ensure Date.prototype.getTimezoneOffset returns consistent value
const originalGetTimezoneOffset = Date.prototype.getTimezoneOffset;
Date.prototype.getTimezoneOffset = function() {
    // Moscow timezone: UTC+3 = -180 minutes
    return -180;
};

// === Performance API (realistic timing) ===
if (window.performance && window.performance.now) {
    const originalNow = window.performance.now;
    let offset = Math.random() * 0.1;
    window.performance.now = function() {
        // Add small jitter to break timing-based fingerprinting
        return originalNow.call(this) + offset;
    };
}

// === Mouse/Touch event consistency ===
// Ensure isTrusted flag is set properly (Playwright sometimes misses this)
const originalAddEventListener = EventTarget.prototype.addEventListener;
EventTarget.prototype.addEventListener = function(type, listener, options) {
    if (typeof listener === 'function' && (type === 'click' || type === 'mousedown' || type === 'mouseup')) {
        const wrappedListener = function(event) {
            Object.defineProperty(event, 'isTrusted', {
                get: () => true,
            });
            return listener.call(this, event);
        };
        return originalAddEventListener.call(this, type, wrappedListener, options);
    }
    return originalAddEventListener.call(this, type, listener, options);
};

// === chrome.runtime.sendMessage (deeper Chrome mimicry) ===
if (window.chrome && window.chrome.runtime) {
    window.chrome.runtime.sendMessage = function() {
        return Promise.resolve();
    };
    window.chrome.runtime.connect = function() {
        return {
            onMessage: { addListener: () => {}, removeListener: () => {} },
            postMessage: () => {},
            disconnect: () => {},
        };
    };
}

// === navigator.userAgentData (Chromium's User-Agent Client Hints) ===
// Yandex SmartCaptcha checks for this modern API
if (!navigator.userAgentData) {
    Object.defineProperty(navigator, 'userAgentData', {
        get: () => ({
            brands: [
                { brand: 'Chromium', version: '120' },
                { brand: 'Google Chrome', version: '120' },
                { brand: 'Not_A Brand', version: '8' },
            ],
            mobile: false,
            platform: 'Windows',
            getHighEntropyValues: (hints) => Promise.resolve({
                architecture: 'x86',
                bitness: '64',
                brands: [
                    { brand: 'Chromium', version: '120' },
                    { brand: 'Google Chrome', version: '120' },
                    { brand: 'Not_A Brand', version: '8' },
                ],
                fullVersionList: [
                    { brand: 'Chromium', version: '120.0.6099.109' },
                    { brand: 'Google Chrome', version: '120.0.6099.109' },
                    { brand: 'Not_A Brand', version: '8.0.0.0' },
                ],
                mobile: false,
                model: '',
                platform: 'Windows',
                platformVersion: '10.0.0',
                uaFullVersion: '120.0.6099.109',
            }),
            toJSON: function() {
                return {
                    brands: this.brands,
                    mobile: this.mobile,
                    platform: this.platform,
                };
            },
        }),
        configurable: true,
    });
}

// === navigator.pdfViewerEnabled (modern Chromium) ===
Object.defineProperty(navigator, 'pdfViewerEnabled', {
    get: () => true,
    configurable: true,
});

// === Intl.DateTimeFormat (timezone consistency) ===
// Ensure timezone matches getTimezoneOffset override
const originalResolvedOptions = Intl.DateTimeFormat.prototype.resolvedOptions;
Intl.DateTimeFormat.prototype.resolvedOptions = function() {
    const options = originalResolvedOptions.call(this);
    options.timeZone = 'Europe/Moscow';
    return options;
};

// === Error.stackTraceLimit (V8 behavior) ===
if (typeof Error.stackTraceLimit === 'undefined') {
    Error.stackTraceLimit = 10;
}

// === Object.getOwnPropertyDescriptor consistency ===
// Ensure our overrides don't look suspicious when inspected
const descriptorCache = new WeakMap();
const originalGetOwnPropertyDescriptor = Object.getOwnPropertyDescriptor;
Object.getOwnPropertyDescriptor = function(obj, prop) {
    const descriptor = originalGetOwnPropertyDescriptor(obj, prop);
    // Make configurable descriptors look natural
    if (descriptor && descriptor.configurable && descriptor.get) {
        if (!descriptorCache.has(descriptor.get)) {
            descriptorCache.set(descriptor.get, true);
        }
    }
    return descriptor;
};

// === Pointer Events (desktop behavior) ===
if (window.PointerEvent) {
    const originalPointerEvent = window.PointerEvent;
    window.PointerEvent = function(type, eventInitDict) {
        if (eventInitDict) {
            // Desktop pointer = mouse (pointerType: 'mouse')
            eventInitDict.pointerType = eventInitDict.pointerType || 'mouse';
        }
        return new originalPointerEvent(type, eventInitDict);
    };
}

// === Media Devices (camera/mic enumeration) ===
if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
    const originalEnumerateDevices = navigator.mediaDevices.enumerateDevices;
    navigator.mediaDevices.enumerateDevices = function() {
        return originalEnumerateDevices.call(this).then(devices => {
            // Return realistic device list (most PCs have at least one of each)
            return [
                { deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' },
                { deviceId: 'communications', kind: 'audioinput', label: '', groupId: 'communications' },
                { deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' },
            ];
        }).catch(() => []);
    };
}

// === Gamepad API (hide automation tell) ===
if (navigator.getGamepads) {
    navigator.getGamepads = function() {
        return [null, null, null, null];
    };
}

// === XR / VR APIs (desktop = no VR) ===
if (navigator.xr) {
    const originalIsSessionSupported = navigator.xr.isSessionSupported;
    navigator.xr.isSessionSupported = function() {
        return Promise.resolve(false);
    };
}

// === requestIdleCallback (present in real browsers) ===
if (!window.requestIdleCallback) {
    window.requestIdleCallback = function(callback, options) {
        const start = Date.now();
        return setTimeout(() => {
            callback({
                didTimeout: false,
                timeRemaining: () => Math.max(0, 50 - (Date.now() - start)),
            });
        }, 1);
    };
    window.cancelIdleCallback = function(id) {
        clearTimeout(id);
    };
}

// === Storage quota (realistic values) ===
if (navigator.storage && navigator.storage.estimate) {
    const originalEstimate = navigator.storage.estimate;
    navigator.storage.estimate = function() {
        return originalEstimate.call(this).then(estimate => ({
            quota: estimate.quota || 299977904102,
            usage: estimate.usage || 1247232,
            usageDetails: estimate.usageDetails || {},
        }));
    };
}

// === Credential Management API ===
if (window.PasswordCredential) {
    // Present but return empty (user hasn't saved passwords on this site)
    if (navigator.credentials && navigator.credentials.get) {
        const originalGet = navigator.credentials.get;
        navigator.credentials.get = function(options) {
            return originalGet.call(this, options).then(() => null).catch(() => null);
        };
    }
}

// === Advanced: Function.prototype.toString consistency ===
// Ensure overridden functions return believable source code
const nativeFunctionCache = new Map();
const originalToString = Function.prototype.toString;
Function.prototype.toString = function() {
    if (nativeFunctionCache.has(this)) {
        return nativeFunctionCache.get(this);
    }
    return originalToString.apply(this, arguments);
};

// Register our overrides as "native code"
const markAsNative = (fn, name) => {
    nativeFunctionCache.set(fn, `function ${name}() { [native code] }`);
};

// Mark key overrides
if (navigator.permissions && navigator.permissions.query) {
    markAsNative(navigator.permissions.query, 'query');
}
if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {
    markAsNative(navigator.mediaDevices.enumerateDevices, 'enumerateDevices');
}

// === iframe detection evasion ===
// Yandex may check if we're running in an iframe
Object.defineProperty(window, 'top', {
    get: () => window,
    configurable: false,
});
Object.defineProperty(window, 'self', {
    get: () => window,
    configurable: false,
});
Object.defineProperty(window, 'parent', {
    get: () => window,
    configurable: false,
});

// === Secure Context indicators ===
Object.defineProperty(window, 'isSecureContext', {
    get: () => true,
    configurable: true,
});

// === navigator.scheduling (modern Chrome API) ===
if (!navigator.scheduling) {
    Object.defineProperty(navigator, 'scheduling', {
        get: () => ({
            isInputPending: () => false,
        }),
        configurable: true,
    });
}

// === window.visualViewport (mobile/desktop consistency) ===
if (!window.visualViewport) {
    Object.defineProperty(window, 'visualViewport', {
        get: () => ({
            width: window.innerWidth,
            height: window.innerHeight,
            offsetLeft: 0,
            offsetTop: 0,
            pageLeft: 0,
            pageTop: 0,
            scale: 1,
            addEventListener: () => {},
            removeEventListener: () => {},
        }),
        configurable: true,
    });
}

// === navigator.locks (modern API) ===
if (!navigator.locks) {
    Object.defineProperty(navigator, 'locks', {
        get: () => ({
            request: (name, callback) => Promise.resolve(callback()),
            query: () => Promise.resolve({ held: [], pending: [] }),
        }),
        configurable: true,
    });
}

// === crypto.randomUUID (modern Chromium) ===
if (window.crypto && !window.crypto.randomUUID) {
    window.crypto.randomUUID = function() {
        return ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
            (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16)
        );
    };
}

// === GPU fingerprinting (WebGL consistency) ===
// Ensure WebGL context returns consistent renderer/vendor across sessions
const webglContextCache = new WeakMap();
const originalGetContext = HTMLCanvasElement.prototype.getContext;
HTMLCanvasElement.prototype.getContext = function(type, ...args) {
    const context = originalGetContext.call(this, type, ...args);
    if (!context) return context;
    
    if (type === 'webgl' || type === 'webgl2' || type === 'experimental-webgl') {
        if (!webglContextCache.has(this)) {
            webglContextCache.set(this, context);
            
            // Override extension getters for consistency
            const getExtension = context.getExtension;
            context.getExtension = function(name) {
                const ext = getExtension.call(this, name);
                if (name === 'WEBGL_debug_renderer_info' && ext) {
                    // Return null to hide debug info
                    return null;
                }
                return ext;
            };
        }
    }
    
    return context;
};

// === navigator.keyboard (modern Chromium) ===
if (!navigator.keyboard) {
    Object.defineProperty(navigator, 'keyboard', {
        get: () => ({
            getLayoutMap: () => Promise.resolve(new Map()),
            lock: () => Promise.resolve(),
            unlock: () => {},
        }),
        configurable: true,
    });
}

// === RTCPeerConnection fingerprinting defense ===
if (window.RTCPeerConnection) {
    const originalRTC = window.RTCPeerConnection;
    window.RTCPeerConnection = function(config) {
        // Randomize ICE candidate IPs slightly to avoid fingerprinting
        const pc = new originalRTC(config);
        const originalCreateOffer = pc.createOffer;
        pc.createOffer = function(options) {
            return originalCreateOffer.call(this, options);
        };
        return pc;
    };
}

// === Speech APIs (present but inactive) ===
if (!window.SpeechRecognition && !window.webkitSpeechRecognition) {
    // Desktop Chrome has this API even if not actively used
    window.SpeechRecognition = function() {};
    window.webkitSpeechRecognition = window.SpeechRecognition;
}

// === Bluetooth/USB APIs (desktop permissions) ===
if (navigator.bluetooth) {
    const originalRequestDevice = navigator.bluetooth.requestDevice;
    navigator.bluetooth.requestDevice = function() {
        return Promise.reject(new DOMException('User cancelled', 'NotFoundError'));
    };
}

if (navigator.usb) {
    const originalRequestDevice = navigator.usb.requestDevice;
    navigator.usb.requestDevice = function() {
        return Promise.reject(new DOMException('User cancelled', 'NotFoundError'));
    };
}

// === Sensor APIs (desktop = mostly unavailable) ===
['Accelerometer', 'Gyroscope', 'LinearAccelerationSensor', 'AbsoluteOrientationSensor'].forEach(name => {
    if (window[name]) {
        const OriginalSensor = window[name];
        window[name] = function() {
            throw new DOMException('Sensor not available', 'NotAllowedError');
        };
    }
});

// === navigator.wakeLock (modern API) ===
if (!navigator.wakeLock) {
    Object.defineProperty(navigator, 'wakeLock', {
        get: () => ({
            request: (type) => Promise.reject(new DOMException('Wake Lock not supported', 'NotSupportedError')),
        }),
        configurable: true,
    });
}

// === Event timing consistency ===
// Ensure events look like they originate from real user interaction
const eventTimestampOffset = Math.random() * 100;
const originalEventConstructor = window.Event;
const EventProxy = new Proxy(originalEventConstructor, {
    construct(target, args) {
        const event = new target(...args);
        // Adjust timestamp to look more natural
        Object.defineProperty(event, 'timeStamp', {
            get: () => performance.now() + eventTimestampOffset,
        });
        return event;
    },
});
window.Event = EventProxy;

// === OffscreenCanvas (modern API) ===
if (typeof OffscreenCanvas === 'undefined') {
    window.OffscreenCanvas = function(width, height) {
        this.width = width;
        this.height = height;
        this.getContext = () => null;
    };
}

// === BroadcastChannel (modern API) ===
if (typeof BroadcastChannel === 'undefined') {
    window.BroadcastChannel = function(name) {
        this.name = name;
        this.postMessage = () => {};
        this.close = () => {};
    };
}

// === Final pass: Property enumeration consistency ===
// Ensure our overrides are enumerable/non-enumerable as expected
const makeNonEnumerable = (obj, prop) => {
    const descriptor = Object.getOwnPropertyDescriptor(obj, prop);
    if (descriptor && descriptor.enumerable) {
        Object.defineProperty(obj, prop, {
            ...descriptor,
            enumerable: false,
        });
    }
};

// Navigator properties should be non-enumerable
['webdriver', 'plugins', 'mimeTypes', 'languages', 'platform', 'vendor'].forEach(prop => {
    makeNonEnumerable(navigator, prop);
});
"""


# Images/fonts/media are the bulk of a reviews page's bytes; review
# photos are never rendered by the scraper — their src urls stay in
# the DOM untouched. resource_type-based routing keeps document /
# script / xhr / stylesheet requests untouched.
BLOCKED_RESOURCE_TYPES = frozenset(
    {"image", "font", "media"}
)

# Route ONLY the asset extensions, not "**/*": every routed request
# detours through this Python process, and routing all ~200 requests
# of a page costs more than the blocked assets save (measured
# 2026-09-16: ~10.5s/page with a catch-all route vs ~8s with
# patterns — see README "Collection speed").
BLOCKED_URL_PATTERNS = (
    "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.webp",
    "**/*.gif", "**/*.avif", "**/*.woff", "**/*.woff2",
    "**/*.ttf", "**/*.mp4",
)


async def install_resource_blocker(
    page: Any,
    *,
    enabled: bool = True,
) -> None:
    """Abort image/font/media requests on ``page``.

    A no-op when ``enabled`` is False. Pattern routes only — see
    :data:`BLOCKED_URL_PATTERNS` for why a catch-all is slower.
    """
    if not enabled:
        return

    async def _route(route: Any) -> None:
        try:
            resource_type = getattr(
                route.request, "resource_type", "",
            )
            if resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()
        except Exception:
            pass

    for pattern in BLOCKED_URL_PATTERNS:
        try:
            # invisible-playwright's Page.route is a coroutine —
            # calling it without await silently drops the route.
            await page.route(pattern, _route)
        except Exception:
            pass
