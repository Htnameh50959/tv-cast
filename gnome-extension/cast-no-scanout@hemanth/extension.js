import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// When a full-screen window (Edge/VLC playing video) is shown with direct
// scanout, mutter skips compositing, so screen casts keep copying an old frame.
// Inhibiting "unredirect" also inhibits direct scanout. We do it only while a
// screen cast / share session (any RemoteAccessHandle) is running.
export default class CastNoScanout extends Extension {
    enable() {
        this._handles = new Map();
        this._inhibited = false;
        this._controller = global.backend.get_remote_access_controller();
        this._newId = this._controller?.connect('new-handle', (_c, handle) => {
            const id = handle.connect('stopped', () => {
                handle.disconnect(id);
                this._handles.delete(handle);
                this._sync();
            });
            this._handles.set(handle, id);
            this._sync();
        }) ?? 0;
    }

    _sync() {
        const want = this._handles.size > 0;
        if (want === this._inhibited)
            return;
        this._inhibited = want;
        if (want)
            Meta.disable_unredirect_for_display(global.display);
        else
            Meta.enable_unredirect_for_display(global.display);
    }

    disable() {
        if (this._newId)
            this._controller.disconnect(this._newId);
        for (const [handle, id] of this._handles)
            handle.disconnect(id);
        this._handles.clear();
        this._sync();
        this._controller = null;
    }
}
