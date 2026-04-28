(function () {
  // Only runs when embedded as an iframe inside DOSM.
  if (window.parent === window) return;

  // Clipboard capture — post copy/paste events to the parent DOSM frame so
  // they can be recorded in the session journal.
  document.addEventListener('copy', function () {
    try {
      var sel = window.getSelection ? window.getSelection().toString() : '';
      if (sel) {
        window.parent.postMessage(
          { type: 'guac_clipboard', direction: 'RDP → local (copy)', content: sel },
          '*'
        );
      }
    } catch (_) {}
  });

  document.addEventListener('paste', function (e) {
    try {
      var text = e.clipboardData ? e.clipboardData.getData('text') : '';
      if (text) {
        window.parent.postMessage(
          { type: 'guac_clipboard', direction: 'local → RDP (paste)', content: text },
          '*'
        );
      }
    } catch (_) {}
  });

  // Keystroke capture — buffer printable chars, emit on Enter.
  // Time-gap heuristic: if the user paused >2 s before starting to type a line
  // AND the line is short (≤32 chars), it is likely a password response —
  // the line is flagged so the parent can redact it before logging.
  var _kBuf = '';
  var _kLastEnterMs = 0;
  var _kLineFirstMs = 0;
  var _K_PAUSE_MS = 2000;
  var _K_PWD_MAX = 32;

  window.addEventListener('keydown', function (e) {
    var now = Date.now();
    var key = e.key;

    if (key === 'Enter') {
      if (_kBuf.length > 0) {
        var pause = (_kLineFirstMs > 0 && _kLastEnterMs > 0)
          ? _kLineFirstMs - _kLastEnterMs : 0;
        var maybePwd = pause > _K_PAUSE_MS && _kBuf.length <= _K_PWD_MAX;
        window.parent.postMessage(
          { type: 'guac_keystroke_line', line: _kBuf, maybe_password: maybePwd },
          '*'
        );
      }
      _kBuf = '';
      _kLineFirstMs = 0;
      _kLastEnterMs = now;
    } else if (key === 'Backspace') {
      _kBuf = _kBuf.slice(0, -1);
    } else if (e.ctrlKey && (key === 'c' || key === 'C')) {
      // Ctrl+C interrupt — log whatever was being typed so the context is
      // preserved, then clear the buffer.
      if (_kBuf.length > 0) {
        window.parent.postMessage(
          { type: 'guac_keystroke_line', line: _kBuf + '^C', maybe_password: false },
          '*'
        );
        _kBuf = '';
        _kLineFirstMs = 0;
      }
      _kLastEnterMs = now;
    } else if (e.ctrlKey && (key === 'u' || key === 'U')) {
      // Ctrl+U — clear line in bash
      _kBuf = '';
      _kLineFirstMs = 0;
    } else if (key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (_kBuf.length === 0) _kLineFirstMs = now;
      _kBuf += key;
    }
  });

  var notified = false;

  function notify() {
    if (notified) return;
    notified = true;
    window.parent.postMessage({ type: 'guac_disconnect' }, '*');
  }

  // Guacamole 1.5 shows a notification panel with a "Reconnect" button when
  // the connection closes (user typed exit, network drop, timeout, etc.).
  // Watch the DOM for that button appearing.
  var observer = new MutationObserver(function () {
    var btns = document.querySelectorAll('button');
    for (var i = 0; i < btns.length; i++) {
      if (/reconnect/i.test(btns[i].textContent)) {
        notify();
        return;
      }
    }
  });

  document.addEventListener('DOMContentLoaded', function () {
    observer.observe(document.body, { childList: true, subtree: true });
  });

  // Also catch hash navigation to '#/' when the user clicks "Home".
  window.addEventListener('hashchange', function () {
    if (window.location.hash === '#/' || window.location.hash === '') {
      notify();
    }
  });
})();
