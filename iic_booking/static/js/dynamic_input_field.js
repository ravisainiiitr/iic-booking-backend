(function($) {
    // Compact periodic table data: [symbol, name, row, col] for main table (1-56, 72-86), then lanthanides (57-71), then actinides (89-103), then 104-118
    var ELEMENTS = [
        ['H','Hydrogen',1,1],['He','Helium',1,18],['Li','Lithium',2,1],['Be','Beryllium',2,2],['B','Boron',2,13],['C','Carbon',2,14],['N','Nitrogen',2,15],['O','Oxygen',2,16],['F','Fluorine',2,17],['Ne','Neon',2,18],
        ['Na','Sodium',3,1],['Mg','Magnesium',3,2],['Al','Aluminum',3,13],['Si','Silicon',3,14],['P','Phosphorus',3,15],['S','Sulfur',3,16],['Cl','Chlorine',3,17],['Ar','Argon',3,18],
        ['K','Potassium',4,1],['Ca','Calcium',4,2],['Sc','Scandium',4,3],['Ti','Titanium',4,4],['V','Vanadium',4,5],['Cr','Chromium',4,6],['Mn','Manganese',4,7],['Fe','Iron',4,8],['Co','Cobalt',4,9],['Ni','Nickel',4,10],['Cu','Copper',4,11],['Zn','Zinc',4,12],['Ga','Gallium',4,13],['Ge','Germanium',4,14],['As','Arsenic',4,15],['Se','Selenium',4,16],['Br','Bromine',4,17],['Kr','Krypton',4,18],
        ['Rb','Rubidium',5,1],['Sr','Strontium',5,2],['Y','Yttrium',5,3],['Zr','Zirconium',5,4],['Nb','Niobium',5,5],['Mo','Molybdenum',5,6],['Tc','Technetium',5,7],['Ru','Ruthenium',5,8],['Rh','Rhodium',5,9],['Pd','Palladium',5,10],['Ag','Silver',5,11],['Cd','Cadmium',5,12],['In','Indium',5,13],['Sn','Tin',5,14],['Sb','Antimony',5,15],['Te','Tellurium',5,16],['I','Iodine',5,17],['Xe','Xenon',5,18],
        ['Cs','Cesium',6,1],['Ba','Barium',6,2],['La','Lanthanum',6,3],['Ce','Cerium',6,3],['Pr','Praseodymium',6,3],['Nd','Neodymium',6,3],['Pm','Promethium',6,3],['Sm','Samarium',6,3],['Eu','Europium',6,3],['Gd','Gadolinium',6,3],['Tb','Terbium',6,3],['Dy','Dysprosium',6,3],['Ho','Holmium',6,3],['Er','Erbium',6,3],['Tm','Thulium',6,3],['Yb','Ytterbium',6,3],['Lu','Lutetium',6,3],['Hf','Hafnium',6,4],['Ta','Tantalum',6,5],['W','Tungsten',6,6],['Re','Rhenium',6,7],['Os','Osmium',6,8],['Ir','Iridium',6,9],['Pt','Platinum',6,10],['Au','Gold',6,11],['Hg','Mercury',6,12],['Tl','Thallium',6,13],['Pb','Lead',6,14],['Bi','Bismuth',6,15],['Po','Polonium',6,16],['At','Astatine',6,17],['Rn','Radon',6,18],
        ['Fr','Francium',7,1],['Ra','Radium',7,2],['Ac','Actinium',7,3],['Th','Thorium',7,3],['Pa','Protactinium',7,3],['U','Uranium',7,3],['Np','Neptunium',7,3],['Pu','Plutonium',7,3],['Am','Americium',7,3],['Cm','Curium',7,3],['Bk','Berkelium',7,3],['Cf','Californium',7,3],['Es','Einsteinium',7,3],['Fm','Fermium',7,3],['Md','Mendelevium',7,3],['No','Nobelium',7,3],['Lr','Lawrencium',7,3],['Rf','Rutherfordium',7,4],['Db','Dubnium',7,5],['Sg','Seaborgium',7,6],['Bh','Bohrium',7,7],['Hs','Hassium',7,8],['Mt','Meitnerium',7,9],['Ds','Darmstadtium',7,10],['Rg','Roentgenium',7,11],['Cn','Copernicium',7,12],['Nh','Nihonium',7,13],['Fl','Flerovium',7,14],['Mc','Moscovium',7,15],['Lv','Livermorium',7,16],['Ts','Tennessine',7,17],['Og','Oganesson',7,18]
    ];

    function buildGrid() {
        var grid = {};
        var lanthanides = [], actinides = [];
        for (var i = 0; i < ELEMENTS.length; i++) {
            var el = ELEMENTS[i];
            var sym = el[0], row = el[2], col = el[3];
            var key = row + '-' + col;
            if (row === 6 && col === 3 && sym !== 'La') { lanthanides.push(el); continue; }
            if (row === 7 && col === 3 && sym !== 'Ac') { actinides.push(el); continue; }
            if (!grid[key]) grid[key] = [];
            grid[key].push(el);
        }
        return { grid: grid, lanthanides: lanthanides, actinides: actinides };
    }

    function openPeriodicTableModal(currentSymbols, disabledSymbols, onApply) {
        var selected = {};
        (currentSymbols || []).forEach(function(s) { selected[s] = true; });
        var disabled = {};
        (disabledSymbols || []).forEach(function(s) { disabled[s] = true; });

        // Ensure disabled symbols are never pre-selected
        Object.keys(disabled).forEach(function(sym) {
            if (selected[sym]) selected[sym] = false;
        });
        var layout = buildGrid();
        var $overlay = $('<div class="periodic-table-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center;"></div>');
        var $modal = $('<div class="periodic-table-modal" style="background:#fff;padding:16px;border-radius:8px;max-width:95vw;max-height:90vh;overflow:auto;box-shadow:0 4px 20px rgba(0,0,0,0.3);"></div>');
        $modal.append('<h3 style="margin:0 0 12px 0;">Select elements</h3>');
        $modal.append('<p class="periodic-selected-summary" style="margin-bottom:12px;font-size:13px;color:#666;">0 selected</p>');

        function renderCell(el, isSelected) {
            var sym = el[0], name = el[1];
            var isDisabled = !!disabled[sym];
            var bg = isDisabled ? '#e5e7eb' : (isSelected ? '#0073aa' : '#f5f5f5');
            var fg = isDisabled ? '#9ca3af' : (isSelected ? '#fff' : '#333');
            var cursor = isDisabled ? 'not-allowed' : 'pointer';
            var border = isDisabled ? '1px dashed #cbd5e1' : '1px solid #ccc';
            var title = isDisabled ? (name + ' (disabled)') : name;
            var $btn = $('<button type="button" class="pt-cell" data-symbol="' + sym + '" title="' + title + '" style="width:32px;height:32px;margin:1px;border:' + border + ';border-radius:4px;cursor:' + cursor + ';font-size:11px;font-weight:bold;background:' + bg + ';color:' + fg + ';' + (isDisabled ? 'opacity:0.85;' : '') + '">' + sym + '</button>');
            if (!isDisabled) {
                $btn.on('click', function() {
                    selected[sym] = !selected[sym];
                    $modal.find('.pt-cell[data-symbol="' + sym + '"]').css({'background': selected[sym] ? '#0073aa' : '#f5f5f5', 'color': selected[sym] ? '#fff' : '#333'});
                    updateSummary();
                });
            }
            return $btn;
        }

        function updateSummary() {
            var count = Object.keys(selected).filter(function(k) { return selected[k]; }).length;
            $modal.find('.periodic-selected-summary').text(count + ' element(s) selected');
        }

        var $table = $('<div style="display:inline-block;"></div>');
        for (var r = 1; r <= 7; r++) {
            var $row = $('<div style="display:flex;"></div>');
            for (var c = 1; c <= 18; c++) {
                var key = r + '-' + c;
                var cells = layout.grid[key];
                if (cells && cells.length) {
                    cells.forEach(function(el) {
                        $row.append(renderCell(el, selected[el[0]]));
                    });
                } else {
                    $row.append($('<div style="width:34px;height:34px;margin:1px;flex-shrink:0;"></div>'));
                }
            }
            $table.append($row);
        }
        var $lanRow = $('<div style="display:flex;margin-top:4px;"></div>');
        layout.lanthanides.forEach(function(el) { $lanRow.append(renderCell(el, selected[el[0]])); });
        $table.append($lanRow);
        var $actRow = $('<div style="display:flex;margin-top:4px;"></div>');
        layout.actinides.forEach(function(el) { $actRow.append(renderCell(el, selected[el[0]])); });
        $table.append($actRow);

        $modal.append($table);
        var $footer = $('<div style="margin-top:16px;display:flex;gap:8px;"></div>');
        $footer.append($('<button type="button" class="button periodic-apply" style="padding:6px 12px;background:#0073aa;color:#fff;border:none;border-radius:4px;cursor:pointer;">Apply</button>'));
        $footer.append($('<button type="button" class="button periodic-cancel" style="padding:6px 12px;background:#ddd;color:#333;border:none;border-radius:4px;cursor:pointer;">Cancel</button>'));
        $modal.append($footer);

        $overlay.append($modal);
        $('body').append($overlay);

        $overlay.on('click', function(e) { if (e.target === $overlay[0]) { $overlay.remove(); } });
        $modal.find('.periodic-cancel').on('click', function() { $overlay.remove(); });
        $modal.find('.periodic-apply').on('click', function() {
            var symbols = Object.keys(selected).filter(function(k) { return selected[k] && !disabled[k]; });
            onApply(symbols);
            $overlay.remove();
        });
        updateSummary();
    }

    function elementSymbolLookup() {
        var bySymbol = {};
        var byName = {};
        for (var i = 0; i < ELEMENTS.length; i++) {
            var sym = ELEMENTS[i][0];
            var name = ELEMENTS[i][1];
            bySymbol[String(sym).toLowerCase()] = sym;
            byName[String(name).toLowerCase()] = sym;
        }
        return { bySymbol: bySymbol, byName: byName };
    }

    function normalizeElementSymbols(lines) {
        // Accept symbols (Fe) or names (Iron). Return unique canonical symbols.
        var lookup = elementSymbolLookup();
        var out = {};
        (lines || []).forEach(function(raw) {
            var line = (raw == null ? '' : String(raw)).trim();
            if (!line) return;
            var cleaned = line.replace(/[,;]+/g, ' ').trim();
            if (!cleaned) return;
            var firstToken = cleaned.split(/\s+/)[0];
            var sym = lookup.bySymbol[String(firstToken).toLowerCase()]
                || lookup.byName[String(cleaned).toLowerCase()]
                || lookup.byName[String(firstToken).toLowerCase()];
            if (sym) out[sym] = true;
        });
        return Object.keys(out);
    }

    function parseDisabledElementsFromHelpText(text) {
        // One per line. Accept symbol (Fe) or full name (Iron). Ignore empty lines.
        var raw = (text || '').split('\n');
        return normalizeElementSymbols(raw);
    }

    function toggleOptionsField() {
        // Iterate each form row (tr containing field_type select), not the formset container,
        // so we use the correct help_text for each periodic table field.
        $('.dynamic-dynamicinputfield_set tr').has('select[id$="-field_type"]').each(function() {
            var $row = $(this);
            var $fieldType = $row.find('select[id$="-field_type"]');
            var $optionsTd = $row.find('td.field-options_text');
            var $optionsCell = $row.find('td').has('textarea[id$="-options_text"]').first();

            if (!$fieldType.length) return;

            var fieldType = $fieldType.val();
            var requiresOptions = ['RADIO', 'COMBO', 'MULTI_SELECT'].indexOf(fieldType) !== -1;
            var isPeriodicTable = fieldType === 'PERIODIC_TABLE';

            if (isPeriodicTable) {
                $optionsTd.show();
                $optionsCell.show();
                var $textarea = $row.find('textarea[id$="-options_text"]');
                var $defaultInput = $row.find('input[id$="-default_value"]');
                if ($textarea.length && !$row.find('.periodic-select-btn').length) {
                    // Keep textarea in DOM and submittable: use visually hidden instead of display:none
                    // so optional text / dynamic field options are always saved on form submit
                    $textarea.css({ visibility: 'hidden', position: 'absolute', left: '-9999px', width: '1px', height: '1px', margin: 0, padding: 0, overflow: 'hidden' });
                    $textarea.attr('tabindex', '-1');
                    var $wrap = $('<div class="periodic-table-field-wrap"></div>');
                    var $btn = $('<button type="button" class="button periodic-select-btn" style="margin-right:8px;">Select elements</button>');
                    var $summary = $('<span class="periodic-selection-summary" style="font-size:12px;color:#666;"></span>');
                    $wrap.append($btn).append($summary);
                    $textarea.after($wrap);

                    function updateRowSummary() {
                        var text = $textarea.val() || '';
                        var currentLines = text.split('\n');
                        // Disabled elements come from Help text (one per line)
                        var helpText = ($row.find('textarea[id$="-help_text"]').val() || '').toString();
                        var disabledSymbols = parseDisabledElementsFromHelpText(helpText);
                        var disabledMap = {};
                        disabledSymbols.forEach(function(s) { disabledMap[s] = true; });

                        var symbols = normalizeElementSymbols(currentLines).filter(function(s) { return !disabledMap[s]; });

                        // If any disabled symbols were present in selection (or non-canonical values), normalize & strip them out
                        var normalizedText = symbols.join('\n');
                        if (($textarea.val() || '') !== normalizedText) {
                            $textarea.val(normalizedText);
                            if ($defaultInput.length) $defaultInput.val(String(symbols.length));
                        }

                        $summary.text(symbols.length + ' element(s) selected' + (symbols.length ? ': ' + symbols.join(', ') : ''));
                    }
                    updateRowSummary();

                    // Keep selection clean when Help text (disabled list) changes
                    $row.off('input.periodicHelpText', 'textarea[id$="-help_text"]');
                    $row.on('input.periodicHelpText', 'textarea[id$="-help_text"]', function() {
                        updateRowSummary();
                    });

                    $btn.on('click', function() {
                        var text = $textarea.val() || '';
                        var currentSymbols = normalizeElementSymbols(text.split('\n'));
                        // Disable elements listed (one per line) in Optional/Help text for this field
                        var helpText = ($row.find('textarea[id$="-help_text"]').val() || '').toString();
                        var disabledSymbols = parseDisabledElementsFromHelpText(helpText);
                        openPeriodicTableModal(currentSymbols, disabledSymbols, function(symbols) {
                            // Ensure we never persist disabled symbols
                            var disabledMap = {};
                            disabledSymbols.forEach(function(s) { disabledMap[s] = true; });
                            var filtered = (symbols || []).filter(function(s) { return !disabledMap[s]; });
                            $textarea.val(filtered.join('\n'));
                            if ($defaultInput.length) $defaultInput.val(String(symbols.length));
                            updateRowSummary();
                        });
                    });
                }
                $row.find('.periodic-selection-summary').each(function() {
                    var text = $row.find('textarea[id$="-options_text"]').val() || '';
                    var helpText = ($row.find('textarea[id$="-help_text"]').val() || '').toString();
                    var disabledSymbols = parseDisabledElementsFromHelpText(helpText);
                    var disabledMap = {};
                    disabledSymbols.forEach(function(s) { disabledMap[s] = true; });
                    var symbols = normalizeElementSymbols(text.split('\n')).filter(function(s) { return !disabledMap[s]; });
                    $(this).text(symbols.length + ' element(s) selected' + (symbols.length ? ': ' + symbols.join(', ') : ''));
                });
            } else if (requiresOptions) {
                $optionsTd.show();
                $optionsCell.show();
                $row.find('.periodic-table-field-wrap').remove();
                $row.find('textarea[id$="-options_text"]').show().css({ visibility: '', position: '', left: '', width: '', height: '', margin: '', padding: '', overflow: '' });
            } else {
                $optionsTd.hide();
                $optionsCell.hide();
                $row.find('.periodic-table-field-wrap').remove();
                $row.find('textarea[id$="-options_text"]').show().css({ visibility: '', position: '', left: '', width: '', height: '', margin: '', padding: '', overflow: '' });
            }
        });
    }

    $(document).ready(function() {
        toggleOptionsField();
        $(document).on('change', 'select[id$="-field_type"]', toggleOptionsField);
        $(document).on('formset:added', function() {
            setTimeout(toggleOptionsField, 100);
        });
    });
})(django.jQuery);
