/**
 * Dynamic field visibility and label updates for ChargeProfileAdmin
 * based on profile_type selection.
 * Handles both standalone ChargeProfile admin and inline formsets.
 */
(function($) {
    'use strict';

    $(document).ready(function() {
        // Get profile_type from Equipment form (for inline) or ChargeProfile form
        function getProfileType() {
            // First try to get from Equipment's profile_type field (for inline)
            var $equipmentProfileType = $('#id_profile_type');
            if ($equipmentProfileType.length) {
                return $equipmentProfileType.val();
            }
            
            // For standalone ChargeProfile admin, get from equipment dropdown
            var $equipment = $('#id_equipment');
            if ($equipment.length) {
                var equipmentId = $equipment.val();
                if (equipmentId) {
                    // Try to get from option data attribute
                    var $selectedOption = $equipment.find('option:selected');
                    if ($selectedOption.length && $selectedOption.data('profile-type')) {
                        return $selectedOption.data('profile-type');
                    }
                }
            }
            
            return null;
        }

        function updateInlineFields($inline, profileType) {
            if (!$inline.length || !profileType) {
                return;
            }

            // Find fields within this specific inline
            var $breakpointField = $inline.find('.field-breakpoint').closest('.form-row, tr, .inline-group');
            var $breakpointLabel = $inline.find('.field-breakpoint label, .field-breakpoint th');
            var $breakpointHelp = $inline.find('.field-breakpoint .help, .field-breakpoint .help-text');
            var $secondaryFlatChargeField = $inline.find('.field-secondary_flat_charge').closest('.form-row, tr, .inline-group');
            var $primaryUnitChargeField = $inline.find('.field-primary_unit_charge').closest('.form-row, tr, .inline-group');
            var $secondaryUnitChargeField = $inline.find('.field-secondary_unit_charge').closest('.form-row, tr, .inline-group');
            var $timeFormulaField = $inline.find('.field-time_formula').closest('.form-row, tr, .inline-group');
            
            // Find or create pricing section description
            // For StackedInline, find the inline-related container
            var $inlineRelated = $primaryUnitChargeField.closest('.inline-related');
            if (!$inlineRelated.length) {
                $inlineRelated = $primaryUnitChargeField.closest('.form-row').parent();
            }
            var $pricingDescription = $inlineRelated.find('.pricing-description');
            
            if (profileType === 'SAMPLE') {
                // SAMPLE: Show primary_unit_charge, breakpoint (Time Limit), secondary_unit_charge, time_formula
                $primaryUnitChargeField.show();
                $breakpointField.show();
                $secondaryUnitChargeField.show();
                $timeFormulaField.show();
                $secondaryFlatChargeField.hide();
                
                if ($breakpointLabel.length) {
                    $breakpointLabel.text('Time Limit:');
                }
                if ($breakpointHelp.length) {
                    $breakpointHelp.text('Time limit in minutes. If exceeded, secondary unit charge is used.');
                }
                
                // Add/update pricing description
                var pricingDesc = '<strong>Pricing Section:</strong><br>' +
                    'Primary unit charge: Charge per sample<br>' +
                    'Time Limit: If time limit is reached, then use secondary unit charge<br>' +
                    'Secondary unit charge: Applied when time limit is exceeded<br><br>' +
                    '<strong>Generic Fields:</strong> A, B, C, D, E, F, G<br>' +
                    '&nbsp;&nbsp;No of sample -> A<br><br>' +
                    '<strong>Time Formula:</strong> Formula based on generic input fields<br><br>' +
                    '<strong>Charge Calculation:</strong> No of Sample * Primary Unit charge. If time limit is reached then No of Sample * Secondary Unit charge.';
                
                if ($pricingDescription.length) {
                    $pricingDescription.html(pricingDesc);
                } else if ($primaryUnitChargeField.length) {
                    // Insert before the primary_unit_charge field
                    $primaryUnitChargeField.before('<div class="pricing-description help" style="margin-bottom: 10px; padding: 10px; background: #f8f9fa; border-left: 3px solid #007cba;">' + pricingDesc + '</div>');
                } else if ($inlineRelated.length) {
                    // Fallback: insert at the beginning of the inline-related container
                    var $fieldsContainer = $inlineRelated.find('.form-row, fieldset');
                    if ($fieldsContainer.length) {
                        $fieldsContainer.first().before('<div class="pricing-description help" style="margin-bottom: 10px; padding: 10px; background: #f8f9fa; border-left: 3px solid #007cba;">' + pricingDesc + '</div>');
                    }
                }
                
                // Update time formula description
                var $timeFormulaHelp = $timeFormulaField.find('.help, .help-text');
                if ($timeFormulaHelp.length) {
                    $timeFormulaHelp.html('Time Formula: Formula based on generic input fields<br>Generic Fields: A (No of sample), B, C, D, E, F, G.');
                }
                
            } else if (profileType === 'HOUR') {
                // HOUR: Show primary_unit_charge, secondary_unit_charge, hide time_formula
                $primaryUnitChargeField.show();
                $secondaryUnitChargeField.show();
                $timeFormulaField.hide();
                $breakpointField.hide();
                $secondaryFlatChargeField.hide();
                
                // Add/update pricing description
                var pricingDesc = '<strong>Pricing Section:</strong><br>' +
                    'Primary unit charge: Charge per hour<br>' +
                    'Secondary unit charge: Additional charge when toggle is enabled<br><br>' +
                    '<strong>Generic Fields:</strong> A, B, C, D, E, F, G<br>' +
                    '&nbsp;&nbsp;No of samples -> A<br>' +
                    '&nbsp;&nbsp;No of slots -> B<br>' +
                    '&nbsp;&nbsp;Toggle -> C<br><br>' +
                    '<strong>Time Formula:</strong> Slot Duration * No of slots<br><br>' +
                    '<strong>Charge Calculation:</strong> (Time per slot/60) * primary unit charge. If enable toggle input then ((Time per slot/60) * primary unit charge) + secondary unit charge.';
                
                if ($pricingDescription.length) {
                    $pricingDescription.html(pricingDesc);
                } else if ($primaryUnitChargeField.length) {
                    // Insert before the primary_unit_charge field
                    $primaryUnitChargeField.before('<div class="pricing-description help" style="margin-bottom: 10px; padding: 10px; background: #f8f9fa; border-left: 3px solid #007cba;">' + pricingDesc + '</div>');
                } else if ($inlineRelated.length) {
                    // Fallback: insert at the beginning of the inline-related container
                    var $fieldsContainer = $inlineRelated.find('.form-row, fieldset');
                    if ($fieldsContainer.length) {
                        $fieldsContainer.first().before('<div class="pricing-description help" style="margin-bottom: 10px; padding: 10px; background: #f8f9fa; border-left: 3px solid #007cba;">' + pricingDesc + '</div>');
                    }
                }
                
            } else if (profileType === 'SAMPLE_ELEMENT') {
                // SAMPLE_ELEMENT: Show primary_unit_charge, breakpoint (Element Limit), secondary_unit_charge, time_formula
                $primaryUnitChargeField.show();
                $breakpointField.show();
                $secondaryUnitChargeField.show();
                $timeFormulaField.show();
                $secondaryFlatChargeField.hide();
                
                if ($breakpointLabel.length) {
                    $breakpointLabel.text('Element Limit:');
                }
                if ($breakpointHelp.length) {
                    $breakpointHelp.text('Maximum number of elements before additional charge applies.');
                }
                
                // Add/update pricing description
                var pricingDesc = '<strong>Pricing Section:</strong><br>' +
                    'Primary unit charge: Charge per sample<br>' +
                    'Element Limit: Maximum elements before additional charge applies<br>' +
                    'Secondary unit charge: Charge per element per sample when limit is exceeded<br><br>' +
                    '<strong>Generic Fields:</strong> A, B, C, D, E, F, G<br>' +
                    '&nbsp;&nbsp;No of samples -> A<br>' +
                    '&nbsp;&nbsp;No of elements -> B<br><br>' +
                    '<strong>Time Formula:</strong> (A * C) + B<br><br>' +
                    '<strong>Primary Charge Calculation:</strong> Let S = (A + (3*C)).<br>' +
                    'If B &lt; element limit then charge = primary unit charge * S.<br>' +
                    'If B &ge; element limit then charge = ((B - element limit) * secondary unit charge * S) + (primary unit charge * S)<br><br>';
                
                if ($pricingDescription.length) {
                    $pricingDescription.html(pricingDesc);
                } else if ($primaryUnitChargeField.length) {
                    // Insert before the primary_unit_charge field
                    $primaryUnitChargeField.before('<div class="pricing-description help" style="margin-bottom: 10px; padding: 10px; background: #f8f9fa; border-left: 3px solid #007cba;">' + pricingDesc + '</div>');
                } else if ($inlineRelated.length) {
                    // Fallback: insert at the beginning of the inline-related container
                    var $fieldsContainer = $inlineRelated.find('.form-row, fieldset');
                    if ($fieldsContainer.length) {
                        $fieldsContainer.first().before('<div class="pricing-description help" style="margin-bottom: 10px; padding: 10px; background: #f8f9fa; border-left: 3px solid #007cba;">' + pricingDesc + '</div>');
                    }
                }
                
                // Update time formula description
                var $timeFormulaHelp = $timeFormulaField.find('.help, .help-text');
                if ($timeFormulaHelp.length) {
                    $timeFormulaHelp.html('Time Formula: (A * C) + B<br>Generic Fields: A (No of samples), B (No of elements), C, D, E, F, G.');
                }
                
            } else if (profileType === 'MULTI_PARAM') {
                // MULTI_PARAM: Hide primary_unit_charge, secondary_unit_charge (time and charge assigned via radio), show breakpoint (as flag), hide time_formula, secondary_flat_charge
                $primaryUnitChargeField.hide();
                $secondaryUnitChargeField.hide();
                $breakpointField.show();
                $timeFormulaField.hide();
                $secondaryFlatChargeField.hide();
                
                if ($breakpointLabel.length) {
                    $breakpointLabel.text('Breakpoint Flag:');
                }
                if ($breakpointHelp.length) {
                    $breakpointHelp.text('Flag to determine if no of sample should be added in charge and time calculation.');
                }
                
                // Add/update pricing description
                var pricingDesc = '<strong>Pricing Section:</strong><br>' +
                    '<strong>Multi param Charge Calculation:</strong><br>' +
                    '&nbsp;&nbsp;Generic Fields -> A, B<br>' +
                    '&nbsp;&nbsp;&nbsp;&nbsp;No of samples -> A<br>' +
                    '&nbsp;&nbsp;&nbsp;&nbsp;No of slots (Radio) -> B<br>' +
                    '&nbsp;&nbsp;Every slot radio have different time and charge per sample<br>' +
                    '&nbsp;&nbsp;Breakpoint should consider the flag for no of sample add in charge and time calculation<br>' +
                    '&nbsp;&nbsp;Time Formula -> if breakpoint flag is true then No of sample * Time per sample else time per sample based on radio input field<br>' +
                    '&nbsp;&nbsp;Charge Calculation -> if breakpoint flag is true then No of sample * Charge per sample else Charge per sample based on radio input field.';
                
                if ($pricingDescription.length) {
                    $pricingDescription.html(pricingDesc);
                } else if ($breakpointField.length) {
                    // Insert before the breakpoint field since primary_unit_charge is hidden
                    $breakpointField.before('<div class="pricing-description help" style="margin-bottom: 10px; padding: 10px; background: #f8f9fa; border-left: 3px solid #007cba;">' + pricingDesc + '</div>');
                } else if ($inlineRelated.length) {
                    // Fallback: insert at the beginning of the inline-related container
                    var $fieldsContainer = $inlineRelated.find('.form-row, fieldset');
                    if ($fieldsContainer.length) {
                        $fieldsContainer.first().before('<div class="pricing-description help" style="margin-bottom: 10px; padding: 10px; background: #f8f9fa; border-left: 3px solid #007cba;">' + pricingDesc + '</div>');
                    }
                }
                
            } else {
                // Other types: Show all fields
                $primaryUnitChargeField.show();
                $breakpointField.show();
                $secondaryUnitChargeField.show();
                $timeFormulaField.show();
                $secondaryFlatChargeField.show();
                
                if ($breakpointLabel.length) {
                    $breakpointLabel.text('Breakpoint:');
                }
                if ($breakpointHelp.length) {
                    $breakpointHelp.text('Breakpoint value for tiered pricing');
                }
                
                // Remove pricing description for other types
                $pricingDescription.remove();
            }
        }

        function updateStandaloneFields(profileType) {
            var $breakpointField = $('.field-breakpoint').closest('.form-row, tr');
            var $breakpointLabel = $('.field-breakpoint label, .field-breakpoint th');
            var $breakpointHelp = $('.field-breakpoint .help, .field-breakpoint .help-text');
            var $primaryUnitChargeField = $('.field-primary_unit_charge').closest('.form-row, tr');
            var $secondaryUnitChargeField = $('.field-secondary_unit_charge').closest('.form-row, tr');
            var $secondaryFlatChargeField = $('.field-secondary_flat_charge').closest('.form-row, tr');
            var $timeFormulaField = $('.field-time_formula').closest('.form-row, tr');
            var $pricingFieldset = $('fieldset.module').has('.field-primary_unit_charge');
            var $timeCalculationFieldset = $('fieldset.module').has('.field-time_formula');

            if (!profileType) {
                // Show all fields by default
                $breakpointField.show();
                $primaryUnitChargeField.show();
                $secondaryUnitChargeField.show();
                $secondaryFlatChargeField.show();
                $timeFormulaField.show();
                return;
            }

            // Update breakpoint field label and visibility
            if (profileType === 'SAMPLE') {
                // SAMPLE: Show breakpoint as "Time Limit"
                $breakpointField.show();
                if ($breakpointLabel.length) {
                    $breakpointLabel.text('Time Limit:');
                }
                if ($breakpointHelp.length) {
                    $breakpointHelp.text('Time limit in minutes. If exceeded, secondary unit charge is used.');
                }
                $secondaryFlatChargeField.hide();
                
                // Update pricing description
                updatePricingDescription(profileType, $pricingFieldset);
                
                // Update time calculation description
                updateTimeCalculationDescription(profileType, $timeCalculationFieldset);
                
            } else if (profileType === 'HOUR') {
                // HOUR: Hide breakpoint and time_formula, show secondary_flat_charge
                $breakpointField.hide();
                $timeFormulaField.hide();
                $secondaryFlatChargeField.show();
                
                // Update pricing description
                updatePricingDescription(profileType, $pricingFieldset);
                
                // Update time calculation description
                updateTimeCalculationDescription(profileType, $timeCalculationFieldset);
                
            } else if (profileType === 'SAMPLE_ELEMENT') {
                // SAMPLE_ELEMENT: Show breakpoint as "Element Limit", show time_formula
                $breakpointField.show();
                $timeFormulaField.show();
                if ($breakpointLabel.length) {
                    $breakpointLabel.text('Element Limit:');
                }
                if ($breakpointHelp.length) {
                    $breakpointHelp.text('Maximum number of elements before additional charge applies.');
                }
                $secondaryFlatChargeField.hide();
                
                // Update pricing description
                updatePricingDescription(profileType, $pricingFieldset);
                
                // Update time calculation description
                updateTimeCalculationDescription(profileType, $timeCalculationFieldset);
                
            } else if (profileType === 'MULTI_PARAM') {
                // MULTI_PARAM: Hide primary_unit_charge, secondary_unit_charge (time and charge assigned via radio), show breakpoint (as flag), hide time_formula, show secondary_flat_charge
                $primaryUnitChargeField.hide();
                $secondaryUnitChargeField.hide();
                $breakpointField.show();
                $timeFormulaField.hide();
                $secondaryFlatChargeField.show();
                
                if ($breakpointLabel.length) {
                    $breakpointLabel.text('Breakpoint Flag:');
                }
                if ($breakpointHelp.length) {
                    $breakpointHelp.text('Flag to determine if no of sample should be added in charge and time calculation.');
                }
                
                // Update pricing description
                updatePricingDescription(profileType, $pricingFieldset);
                
                // Update time calculation description
                updateTimeCalculationDescription(profileType, $timeCalculationFieldset);
                
            } else {
                // Other types: Show all fields
                $breakpointField.show();
                $timeFormulaField.show();
                if ($breakpointLabel.length) {
                    $breakpointLabel.text('Breakpoint:');
                }
                if ($breakpointHelp.length) {
                    $breakpointHelp.text('Breakpoint value for tiered pricing');
                }
                $secondaryFlatChargeField.show();
                
                // Reset descriptions
                updatePricingDescription(null, $pricingFieldset);
                updateTimeCalculationDescription(null, $timeCalculationFieldset);
            }
        }

        function updatePricingDescription(profileType, $pricingFieldset) {
            if (!$pricingFieldset.length) return;
            
            var $pricingDescription = $pricingFieldset.find('.description');
            
            if (!profileType) {
                $pricingDescription.text('');
                return;
            }
        }

        function updateTimeCalculationDescription(profileType, $timeCalculationFieldset) {
            if (!$timeCalculationFieldset.length) return;
            
            var $timeFormulaHelp = $timeCalculationFieldset.find('.description');
            
            if (!profileType) {
                if ($timeFormulaHelp.length) {
                    $timeFormulaHelp.text('Formula for time calculation (e.g., "(A * C) + B"). Use field keys A-G.');
                }
                return;
            }

            var descriptions = {
                'SAMPLE': 'Time Formula: (A * C) + B. Generic Fields: A, B, C, D, E, F, G.',
                'HOUR': 'Time Formula: Slot Duration * No of slots. Generic Fields: A (No of samples), B (No of slots), C (Toggle), D, E, F, G.',
                'SAMPLE_ELEMENT': 'Time Formula: (A * C) + B. Generic Fields: A (No of samples), B (No of elements), C, D, E, F, G.'
            };

            if (descriptions[profileType] && $timeFormulaHelp.length) {
                $timeFormulaHelp.text(descriptions[profileType]);
            }
        }

        function updateAllChargeProfiles() {
            var profileType = getProfileType();
            
            // Update standalone ChargeProfile admin (if exists)
            if ($('#chargeprofile_form').length || $('form').has('.field-primary_unit_charge').not('.inline-group').length) {
                updateStandaloneFields(profileType);
            }
            
            // Update all inline ChargeProfile formsets
            $('.inline-group').each(function() {
                var $inline = $(this);
                // Check if this is a ChargeProfile inline (has breakpoint field)
                if ($inline.find('.field-breakpoint').length) {
                    updateInlineFields($inline, profileType);
                }
            });
        }

        // Initialize on page load
        updateAllChargeProfiles();
        var initialProfileType = getProfileType();
        updateMultiParamInline(initialProfileType);

        // Function to show/hide MultiParamDefinitionInline based on profile_type
        function updateMultiParamInline(profileType) {
            // Find the MultiParamDefinitionInline section
            // It will have a heading with "Slot Option Configuration" or "Multi-Parameter Definitions"
            var $multiParamInline = $('.inline-group').filter(function() {
                var $heading = $(this).find('h2, h3, .inline-related h2');
                var headingText = $heading.text().toLowerCase();
                return headingText.indexOf('slot option') !== -1 || 
                       headingText.indexOf('multi-parameter') !== -1 ||
                       headingText.indexOf('multi parameter') !== -1;
            });
            
            if (profileType === 'MULTI_PARAM') {
                $multiParamInline.show();
            } else {
                $multiParamInline.hide();
            }
        }

        // Update when Equipment's profile_type changes (for inline)
        $('#id_profile_type').on('change', function() {
            var profileType = $(this).val();
            updateAllChargeProfiles();
            updateMultiParamInline(profileType);
        });

        // Update when equipment dropdown changes (for standalone ChargeProfile admin)
        $('#id_equipment').on('change', function() {
            updateAllChargeProfiles();
        });

        // Handle formset additions (when new inline is added)
        $(document).on('formset:added', function(event, $row) {
            if ($row == null) return;
            var $el = $row.jquery ? $row : $($row);
            var $inline = $el.closest('.inline-group');
            if ($inline.length && $inline.find('.field-breakpoint').length) {
                updateInlineFields($inline, getProfileType());
            }
        });
    });

})(django.jQuery);

