/* Advanced table (TYPED_TABLE) column builder for the Django admin dynamic input field rows.
 * The schema is kept as JSON in the row's hidden table_config input; the server re-validates it on save. */
(function ($) {
    var COLUMN_TYPES = [
        ['NUMERIC', 'Numeric'],
        ['TEXT', 'Text'],
        ['RADIO', 'Radio'],
        ['COMBO', 'Combobox (dropdown)'],
        ['MULTI_SELECT', 'Multi-select'],
        ['TOGGLE', 'Toggle (Yes/No)'],
        ['PERIODIC_TABLE', 'Periodic table']
    ];
    var CHOICE_TYPES = ['RADIO', 'COMBO', 'MULTI_SELECT'];
    var MAX_COLUMNS = 20;
    var MAX_ROWS_CAP = 200;

    function esc(text) {
        return $('<div>').text(text == null ? '' : String(text)).html();
    }

    function slugify(label) {
        var slug = String(label || '').toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
        if (!slug) slug = 'col';
        if (!/^[a-z]/.test(slug)) slug = 'c_' + slug;
        return slug.slice(0, 40);
    }

    function parseConfig(raw) {
        var cfg = {};
        try { cfg = raw ? JSON.parse(raw) : {}; } catch (e) { cfg = {}; }
        if (!cfg || typeof cfg !== 'object') cfg = {};
        var rows = cfg.rows && typeof cfg.rows === 'object' ? cfg.rows : {};
        return {
            version: 1,
            columns: Array.isArray(cfg.columns) ? cfg.columns.map(function (c) { return $.extend({}, c); }) : [],
            rows: {
                mode: rows.mode === 'LINKED' ? 'LINKED' : 'USER',
                link_field_key: rows.link_field_key || '',
                min_rows: rows.min_rows != null ? rows.min_rows : 0,
                max_rows: rows.max_rows != null ? rows.max_rows : 50,
                initial_rows: rows.initial_rows != null ? rows.initial_rows : 1,
                serial_column: rows.serial_column !== false,
                allow_duplicate: rows.allow_duplicate !== false
            }
        };
    }

    function summaryText(cfg) {
        var n = cfg.columns.length;
        if (!n) return 'No columns yet';
        var rows = cfg.rows.mode === 'LINKED'
            ? 'rows follow field ' + (cfg.rows.link_field_key || '?')
            : 'user adds rows (max ' + (cfg.rows.max_rows || 50) + ')';
        return n + ' column' + (n === 1 ? '' : 's') + ' · ' + rows;
    }

    function rowOf($el) { return $el.closest('tr'); }

    function siblingNumericFields($row) {
        var userType = $row.find('input[name$="-user_type"]').val() || '';
        var ownKey = $row.find('[name$="-field_key"]').val() || '';
        var out = [];
        $('input[name$="-table_config"]').each(function () {
            var $r = rowOf($(this));
            if ($r.find('input[name$="-DELETE"]').is(':checked')) return;
            if (($r.find('input[name$="-user_type"]').val() || '') !== userType) return;
            var type = $r.find('select[name$="-field_type"]').val();
            var key = $r.find('[name$="-field_key"]').val();
            if (type === 'NUMERIC' && key && key !== ownKey) {
                out.push({ key: key, label: $r.find('input[name$="-field_label"]').val() || key });
            }
        });
        return out.sort(function (a, b) { return a.key < b.key ? -1 : 1; });
    }

    function validate(cfg, ownKey) {
        var errors = [];
        if (!cfg.columns.length) errors.push('Add at least one column.');
        if (cfg.columns.length > MAX_COLUMNS) errors.push('At most ' + MAX_COLUMNS + ' columns.');
        var seen = {};
        cfg.columns.forEach(function (c, i) {
            var name = c.label ? '"' + c.label + '"' : 'Column ' + (i + 1);
            if (!c.label) errors.push('Column ' + (i + 1) + ' needs a label.');
            var key = c.key || slugify(c.label);
            if (!/^[a-z][a-z0-9_]{0,39}$/.test(key)) errors.push(name + ': key must start with a letter and use a-z, 0-9, _.');
            if (seen[key]) errors.push('Column key "' + key + '" is used twice.');
            seen[key] = true;
            if (CHOICE_TYPES.indexOf(c.type) !== -1 && !(c.options || []).length) errors.push(name + ' needs at least one option.');
            if (c.type === 'NUMERIC') {
                var lo = c.min === '' || c.min == null ? null : Number(c.min);
                var hi = c.max === '' || c.max == null ? null : Number(c.max);
                if (lo !== null && isNaN(lo)) errors.push(name + ': lower limit must be a number.');
                if (hi !== null && isNaN(hi)) errors.push(name + ': upper limit must be a number.');
                if (lo !== null && hi !== null && lo > hi) errors.push(name + ': lower limit is above the upper limit.');
                if (c.step !== '' && c.step != null && !(Number(c.step) > 0)) errors.push(name + ': step must be greater than 0.');
            }
        });
        var r = cfg.rows;
        if (r.mode === 'LINKED') {
            if (!r.link_field_key) errors.push('Choose the numeric field that sets the number of rows.');
            if (r.link_field_key && r.link_field_key === ownKey) errors.push('Rows cannot be linked to the table itself.');
        } else if (Number(r.min_rows) > Number(r.max_rows)) {
            errors.push('Minimum rows cannot be greater than maximum rows.');
        }
        if (!(Number(r.max_rows) >= 1 && Number(r.max_rows) <= MAX_ROWS_CAP)) errors.push('Maximum rows must be 1–' + MAX_ROWS_CAP + '.');
        return errors;
    }

    function cleanForSave(cfg) {
        var used = {};
        var columns = cfg.columns.map(function (c) {
            var key = c.key || slugify(c.label);
            var n = 2;
            while (!c.key && used[key]) { key = slugify(c.label).slice(0, 36) + '_' + n; n++; }
            used[key] = true;
            var out = { key: key, label: c.label, type: c.type, required: !!c.required, help_text: c.help_text || '' };
            if (CHOICE_TYPES.indexOf(c.type) !== -1) out.options = c.options || [];
            if (c.type === 'NUMERIC') {
                out.min = c.min === '' ? null : c.min;
                out.max = c.max === '' ? null : c.max;
                out.step = c.step === '' ? null : c.step;
                out.integer = !!c.integer;
            }
            if (c.type === 'TEXT') out.max_length = c.max_length === '' ? null : c.max_length;
            if (c.type !== 'PERIODIC_TABLE' && c.default !== '' && c.default != null) out.default = c.default;
            return out;
        });
        var r = cfg.rows;
        return {
            version: 1,
            columns: columns,
            rows: {
                mode: r.mode,
                link_field_key: r.mode === 'LINKED' ? r.link_field_key : null,
                min_rows: Number(r.min_rows) || 0,
                max_rows: Number(r.max_rows) || 50,
                initial_rows: Number(r.initial_rows) || 0,
                serial_column: !!r.serial_column,
                allow_duplicate: !!r.allow_duplicate
            }
        };
    }

    function previewCell(c) {
        var dis = ' disabled aria-hidden="true" tabindex="-1"';
        if (c.type === 'NUMERIC') {
            var hint = [c.min !== '' && c.min != null ? 'min ' + c.min : '', c.max !== '' && c.max != null ? 'max ' + c.max : ''].filter(Boolean).join(' · ');
            return '<input type="number" style="width:90px"' + dis + ' placeholder="' + esc(hint) + '">';
        }
        if (c.type === 'TEXT') return '<input type="text" style="width:120px"' + dis + ' placeholder="' + esc(c.help_text || '') + '">';
        if (c.type === 'TOGGLE') return '<input type="checkbox"' + dis + '> Yes';
        if (c.type === 'PERIODIC_TABLE') return '<button type="button" class="button"' + dis + '>Select elements</button>';
        var opts = (c.options || []).slice(0, 4);
        if (c.type === 'RADIO') return opts.map(function (o) { return '<label style="margin-right:6px"><input type="radio"' + dis + '> ' + esc(o) + '</label>'; }).join('');
        if (c.type === 'MULTI_SELECT') return opts.map(function (o) { return '<label style="margin-right:6px"><input type="checkbox"' + dis + '> ' + esc(o) + '</label>'; }).join('');
        return '<select' + dis + '><option>' + esc(opts[0] || 'Choose…') + '</option></select>';
    }

    function renderPreview($box, cfg) {
        if (!cfg.columns.length) { $box.html('<p style="color:#666;margin:0">Add a column to see the preview.</p>'); return; }
        var head = (cfg.rows.serial_column ? '<th>S.No.</th>' : '') + cfg.columns.map(function (c) {
            return '<th>' + esc(c.label || '(no label)') + (c.required ? ' <span style="color:#b91c1c">*</span>' : '') + '</th>';
        }).join('');
        var body = '';
        for (var i = 1; i <= 2; i++) {
            body += '<tr>' + (cfg.rows.serial_column ? '<td>' + i + '</td>' : '') + cfg.columns.map(function (c) { return '<td>' + previewCell(c) + '</td>'; }).join('') + '</tr>';
        }
        var note = cfg.rows.mode === 'LINKED'
            ? 'Rows follow field ' + esc(cfg.rows.link_field_key || '?') + ' (up to ' + esc(cfg.rows.max_rows) + ').'
            : 'Users add / remove rows (' + esc(cfg.rows.min_rows) + '–' + esc(cfg.rows.max_rows) + ' rows, ' + esc(cfg.rows.initial_rows) + ' shown at first).';
        $box.html('<div style="overflow:auto"><table class="tt-preview" style="border-collapse:collapse;min-width:100%">' +
            '<thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table></div>' +
            '<p style="color:#666;margin:6px 0 0">' + note + '</p>');
        $box.find('th,td').css({ border: '1px solid #e5e7eb', padding: '4px 6px', textAlign: 'left', fontSize: '12px' });
    }

    function columnEditor(c, index, total) {
        var typeOptions = COLUMN_TYPES.map(function (t) {
            return '<option value="' + t[0] + '"' + (c.type === t[0] ? ' selected' : '') + '>' + t[1] + '</option>';
        }).join('');
        var html = '<fieldset class="tt-col" data-index="' + index + '" style="border:1px solid #ddd;border-radius:6px;padding:8px;margin:0 0 8px">' +
            '<legend style="font-weight:600;padding:0 4px">Column ' + (index + 1) + '</legend>' +
            '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end">' +
            '<label>Label<br><input type="text" data-prop="label" value="' + esc(c.label) + '" maxlength="100" style="width:180px"></label>' +
            '<label>Key<br><input type="text" data-prop="key" value="' + esc(c.key) + '" placeholder="' + esc(slugify(c.label)) + '" maxlength="40" style="width:120px"></label>' +
            '<label>Type<br><select data-prop="type">' + typeOptions + '</select></label>' +
            '<label><input type="checkbox" data-prop="required"' + (c.required ? ' checked' : '') + '> Required</label>' +
            '<span style="margin-left:auto">' +
            '<button type="button" class="button tt-up" aria-label="Move column up"' + (index === 0 ? ' disabled' : '') + '>↑</button> ' +
            '<button type="button" class="button tt-down" aria-label="Move column down"' + (index === total - 1 ? ' disabled' : '') + '>↓</button> ' +
            '<button type="button" class="button tt-remove" aria-label="Remove column" style="color:#b91c1c">Remove</button>' +
            '</span></div>' +
            '<div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:6px">';
        if (c.type === 'NUMERIC') {
            html += '<label>Lower limit<br><input type="number" step="any" data-prop="min" value="' + esc(c.min) + '" style="width:90px"></label>' +
                '<label>Upper limit<br><input type="number" step="any" data-prop="max" value="' + esc(c.max) + '" style="width:90px"></label>' +
                '<label>Step<br><input type="number" step="any" data-prop="step" value="' + esc(c.step) + '" style="width:80px"></label>' +
                '<label style="align-self:flex-end"><input type="checkbox" data-prop="integer"' + (c.integer ? ' checked' : '') + '> Whole numbers only</label>';
        }
        if (c.type === 'TEXT') {
            html += '<label>Max length<br><input type="number" min="1" max="500" data-prop="max_length" value="' + esc(c.max_length) + '" style="width:80px"></label>';
        }
        if (CHOICE_TYPES.indexOf(c.type) !== -1) {
            html += '<label>Options (one per line)<br><textarea data-prop="options" rows="3" style="width:200px">' + esc((c.options || []).join('\n')) + '</textarea></label>';
        }
        if (c.type !== 'PERIODIC_TABLE') {
            var dflt = c.type === 'MULTI_SELECT' && Array.isArray(c.default) ? c.default.join(', ') : (c.default === true ? 'yes' : c.default === false ? '' : c.default);
            html += '<label>Default' + (c.type === 'TOGGLE' ? ' (yes / no)' : c.type === 'MULTI_SELECT' ? ' (comma separated)' : '') +
                '<br><input type="text" data-prop="default" value="' + esc(dflt) + '" style="width:120px"></label>';
        }
        html += '<label>Help / placeholder<br><input type="text" data-prop="help_text" value="' + esc(c.help_text) + '" maxlength="300" style="width:200px"></label>' +
            '</div></fieldset>';
        return html;
    }

    function openBuilder($row, $hidden, $summary) {
        var cfg = parseConfig($hidden.val());
        var ownKey = $row.find('[name$="-field_key"]').val() || '';
        var fieldLabel = $row.find('input[name$="-field_label"]').val() || 'Advanced table';
        var $overlay = $('<div class="tt-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;display:flex;align-items:center;justify-content:center"></div>');
        var $modal = $('<div class="tt-modal" role="dialog" aria-modal="true" aria-labelledby="tt-title" style="background:#fff;color:#111;padding:16px;border-radius:8px;width:min(980px,96vw);max-height:92vh;overflow:auto;box-shadow:0 8px 30px rgba(0,0,0,.3)"></div>');
        $modal.append('<h2 id="tt-title" style="margin:0 0 4px">Advanced table: ' + esc(fieldLabel) + ' (' + esc(ownKey || '?') + ')</h2>' +
            '<p style="margin:0 0 12px;color:#555">Each column has its own type and limits. Users see this as a table on the booking page (cards on phones).</p>');
        var $cols = $('<div class="tt-cols"></div>');
        var $addCol = $('<button type="button" class="button tt-add-col">+ Add column</button>');
        var $rows = $('<div class="tt-rows" style="border:1px solid #ddd;border-radius:6px;padding:8px;margin-top:12px"></div>');
        var $preview = $('<div class="tt-preview-box" style="margin-top:12px;border:1px dashed #cbd5e1;border-radius:6px;padding:8px"></div>');
        var $errors = $('<div class="tt-errors" role="alert" style="color:#b91c1c;margin-top:8px"></div>');
        var $footer = $('<div style="margin-top:12px;display:flex;gap:8px;justify-content:flex-end">' +
            '<button type="button" class="button tt-cancel">Cancel</button>' +
            '<button type="button" class="button default tt-save" style="background:#0073aa;color:#fff">Save columns</button></div>');

        function renderCols() {
            $cols.html(cfg.columns.map(function (c, i) { return columnEditor(c, i, cfg.columns.length); }).join(''));
            $addCol.prop('disabled', cfg.columns.length >= MAX_COLUMNS);
            renderPreview($preview, cfg);
        }

        function renderRows() {
            var numeric = siblingNumericFields($row);
            var linkOptions = '<option value="">Choose a numeric field…</option>' + numeric.map(function (f) {
                return '<option value="' + esc(f.key) + '"' + (cfg.rows.link_field_key === f.key ? ' selected' : '') + '>' + esc(f.key + ' — ' + f.label) + '</option>';
            }).join('');
            if (cfg.rows.link_field_key && !numeric.some(function (f) { return f.key === cfg.rows.link_field_key; })) {
                linkOptions += '<option value="' + esc(cfg.rows.link_field_key) + '" selected>' + esc(cfg.rows.link_field_key) + ' (not a numeric field here)</option>';
            }
            var linked = cfg.rows.mode === 'LINKED';
            $rows.html('<strong>Rows</strong>' +
                '<div style="display:flex;flex-wrap:wrap;gap:12px;margin-top:6px;align-items:flex-end">' +
                '<label><input type="radio" name="tt-mode" value="USER"' + (!linked ? ' checked' : '') + '> User adds / removes rows</label>' +
                '<label><input type="radio" name="tt-mode" value="LINKED"' + (linked ? ' checked' : '') + '> Rows follow a numeric field</label>' +
                (linked ? '<label>Linked field<br><select data-row="link_field_key">' + linkOptions + '</select></label>' : '') +
                (!linked ? '<label>Min rows<br><input type="number" min="0" max="200" data-row="min_rows" value="' + esc(cfg.rows.min_rows) + '" style="width:70px"></label>' +
                    '<label>Rows shown at first<br><input type="number" min="0" max="200" data-row="initial_rows" value="' + esc(cfg.rows.initial_rows) + '" style="width:70px"></label>' : '') +
                '<label>Max rows<br><input type="number" min="1" max="200" data-row="max_rows" value="' + esc(cfg.rows.max_rows) + '" style="width:70px"></label>' +
                '<label><input type="checkbox" data-row="serial_column"' + (cfg.rows.serial_column ? ' checked' : '') + '> Show S.No. column</label>' +
                (!linked ? '<label><input type="checkbox" data-row="allow_duplicate"' + (cfg.rows.allow_duplicate ? ' checked' : '') + '> Allow “duplicate row”</label>' : '') +
                '</div>' +
                (linked ? '<p style="margin:6px 0 0;color:#555">The table always has as many rows as the linked field’s value in the same sample set (capped at Max rows). Lowering it hides the extra rows; raising it again brings them back.</p>' : ''));
            renderPreview($preview, cfg);
        }

        $cols.on('input change', '[data-prop]', function () {
            var $el = $(this);
            var i = Number($el.closest('.tt-col').data('index'));
            var prop = $el.data('prop');
            var c = cfg.columns[i];
            if (!c) return;
            if ($el.is(':checkbox')) c[prop] = $el.is(':checked');
            else if (prop === 'options') c.options = $el.val().split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
            else if (prop === 'default' && c.type === 'MULTI_SELECT') c.default = $el.val().split(',').map(function (s) { return s.trim(); }).filter(Boolean);
            else if (prop === 'default' && c.type === 'TOGGLE') c.default = /^(y|yes|true|1|on)$/i.test($el.val().trim());
            else c[prop] = $el.val();
            if (prop === 'type') { renderCols(); return; }
            if (prop === 'label') $el.closest('.tt-col').find('[data-prop="key"]').attr('placeholder', slugify(c.label));
            renderPreview($preview, cfg);
        });
        $cols.on('click', '.tt-remove, .tt-up, .tt-down', function () {
            var i = Number($(this).closest('.tt-col').data('index'));
            if ($(this).hasClass('tt-remove')) cfg.columns.splice(i, 1);
            else {
                var j = $(this).hasClass('tt-up') ? i - 1 : i + 1;
                if (j < 0 || j >= cfg.columns.length) return;
                var tmp = cfg.columns[i]; cfg.columns[i] = cfg.columns[j]; cfg.columns[j] = tmp;
            }
            renderCols();
        });
        $addCol.on('click', function () {
            cfg.columns.push({ label: '', key: '', type: 'TEXT', required: false, help_text: '' });
            renderCols();
            $cols.find('.tt-col').last().find('[data-prop="label"]').trigger('focus');
        });
        $rows.on('change input', 'input[name="tt-mode"], [data-row]', function () {
            var $el = $(this);
            if ($el.attr('name') === 'tt-mode') { cfg.rows.mode = $el.val(); renderRows(); return; }
            var prop = $el.data('row');
            cfg.rows[prop] = $el.is(':checkbox') ? $el.is(':checked') : $el.val();
            renderPreview($preview, cfg);
        });

        function close() { $overlay.remove(); $(document).off('keydown.ttBuilder'); }
        $footer.find('.tt-cancel').on('click', close);
        $footer.find('.tt-save').on('click', function () {
            var errors = validate(cfg, ownKey);
            if (errors.length) {
                $errors.html('<ul style="margin:0;padding-left:18px">' + errors.map(function (e) { return '<li>' + esc(e) + '</li>'; }).join('') + '</ul>');
                return;
            }
            var saved = cleanForSave(cfg);
            $hidden.val(JSON.stringify(saved)).trigger('change');
            $summary.text(summaryText(parseConfig($hidden.val())));
            close();
        });
        $(document).on('keydown.ttBuilder', function (e) { if (e.key === 'Escape') close(); });

        $modal.append('<h3 style="margin:0 0 6px">Columns</h3>').append($cols).append($addCol).append($rows)
            .append('<h3 style="margin:12px 0 0">Preview</h3>').append($preview).append($errors).append($footer);
        $overlay.append($modal);
        $('body').append($overlay);
        renderCols();
        renderRows();
        $modal.find('input,select,textarea,button').first().trigger('focus');
    }

    function syncRow($row) {
        var $type = $row.find('select[name$="-field_type"]');
        var $hidden = $row.find('input[name$="-table_config"]');
        if (!$type.length || !$hidden.length) return;
        var $textarea = $row.find('textarea[name$="-options_text"]');
        var $cell = $textarea.closest('td');
        var isTyped = $type.val() === 'TYPED_TABLE';
        $row.find('.tt-builder-wrap').remove();
        if (!isTyped) return;
        $cell.show();
        $textarea.hide();
        var $wrap = $('<div class="tt-builder-wrap"></div>');
        var $btn = $('<button type="button" class="button tt-open">Configure columns</button>');
        var $summary = $('<div class="tt-summary" style="font-size:12px;color:#555;margin-top:4px"></div>');
        $summary.text(summaryText(parseConfig($hidden.val())));
        $wrap.append($btn).append($summary);
        ($cell.length ? $cell : $type.closest('td')).append($wrap);
        $btn.on('click', function () { openBuilder($row, $hidden, $summary); });
    }

    function syncAll() {
        $('input[name$="-table_config"]').each(function () { syncRow(rowOf($(this))); });
    }

    $(function () {
        syncAll();
        $(document).on('change', 'select[name$="-field_type"]', function () {
            var $row = rowOf($(this));
            setTimeout(function () {
                if ($(this).val() !== 'TYPED_TABLE') $row.find('textarea[name$="-options_text"]').show();
                syncRow($row);
            }.bind(this), 0);
        });
        $(document).on('formset:added', function () { setTimeout(syncAll, 150); });
    });
})(django.jQuery);
