namespace Vool.WindowsHost;

internal static class BridgeScript
{
    internal const string Source = """
        (() => {
          if (window !== window.top || location.origin !== 'http://127.0.0.1:11435') return;
          const pending = new Map();
          const nextId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)),
            byte => byte.toString(16).padStart(2, '0')).join('');
          chrome.webview.addEventListener('message', event => {
            const reply = event.data;
            if (!reply || reply.channel !== 'vool-native-v1' || typeof reply.id !== 'string') return;
            const done = pending.get(reply.id);
            if (!done) return;
            pending.delete(reply.id);
            done(reply.result);
          });
          const call = (method, args) => new Promise(resolve => {
            const id = nextId();
            pending.set(id, resolve);
            chrome.webview.postMessage({channel: 'vool-native-v1', id, method, args});
          });
          const api = Object.freeze({
            open_settings: section => call('open_settings', [section || '']),
            close_settings: () => call('close_settings', []),
            open_setup: step => call('open_setup', [step || '']),
            close_setup: () => call('close_setup', []),
            pick_folder: () => call('pick_folder', []),
            save_answer_pdf: (session, request) => call('save_answer_pdf', [session, request]),
            save_chat_export: (session, format, timestamps, attachments) =>
              call('save_chat_export', [session, format, timestamps, attachments]),
            open_companion_appearance: () => call('open_companion_appearance', []),
            detach_companion: payload => call('detach_companion', payload === undefined ? [] : [payload]),
            sync_companion: payload => call('sync_companion', payload === undefined ? [] : [payload]),
            pet_yield_rect: rect => call('pet_yield_rect', [rect || null]),
            hide_companion: () => call('hide_companion', []),
            reset_companion_position: () => call('reset_companion_position', []),
            attach_companion: () => call('attach_companion', []),
          });
          Object.defineProperty(window, 'pywebview', {
            value: Object.freeze({api}), writable: false, configurable: false,
          });
          if (location.pathname === '/companion') {
            document.addEventListener('pointerdown', event => {
              if (event.button === 0 && event.target && event.target.id === 'pet')
                call('begin_companion_drag', []);
            }, true);
          }
        })();
        """;
}
