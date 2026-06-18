(function () {
  // Only runs when embedded as an iframe inside DOSM.
  if (window.parent === window) return;

  // Clipboard capture - post copy/paste events to the parent DOSM frame so
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

  // Keystroke capture - buffer printable chars, emit on Enter.
  // The user explicitly opted in via the "Guac keystrokes" recording option,
  // which warns that passwords may appear. No automatic redaction is done
  // here; pause/length heuristics produced too many false positives (any
  // brief pause to think before a short command got masked).
  var _kBuf = '';

  window.addEventListener('keydown', function (e) {
    var key = e.key;

    if (key === 'Enter') {
      if (_kBuf.length > 0) {
        window.parent.postMessage(
          { type: 'guac_keystroke_line', line: _kBuf },
          '*'
        );
      }
      _kBuf = '';
    } else if (key === 'Backspace') {
      _kBuf = _kBuf.slice(0, -1);
    } else if (e.ctrlKey && (key === 'c' || key === 'C')) {
      // Ctrl+C interrupt - log whatever was being typed so the context is
      // preserved, then clear the buffer.
      if (_kBuf.length > 0) {
        window.parent.postMessage(
          { type: 'guac_keystroke_line', line: _kBuf + '^C' },
          '*'
        );
        _kBuf = '';
      }
    } else if (e.ctrlKey && (key === 'u' || key === 'U')) {
      // Ctrl+U - clear line in bash
      _kBuf = '';
    } else if (key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
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
