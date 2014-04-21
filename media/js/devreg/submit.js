(function(exports) {
    "use strict";

    exports.houdini = function() {
        // Initialize magic labels.
        $(document).delegate('.houdini.ready .edit', 'click', _pd(function(e) {
            var $label = $(this).closest('.houdini');
            $label.addClass('fading');
            setTimeout(function() {
                $label.removeClass('ready').addClass('active');
            }, 500);
        })).delegate('.houdini.active .done', 'click', _pd(function(e) {
            var $label = $(this).closest('.houdini');
            $label.removeClass('active').addClass('ready');
            // Replace text with new value.
            $label.find('.output').text($label.find('input').val());
        }));
    };

    // Handle Name and Slug.
    exports.nameHoudini = function() {
        var $ctx = $('#general-details');
    };

    exports.privacy = function() {
        // Privacy Policy is required. Maybe I can reuse this elsewhere.
        var $ctx = $('#show-privacy');
        // When the checkbox is clicked ...
        $ctx.delegate('input[type=checkbox]', 'click', function() {
            // Hide the label ...
            $ctx.find('label.checkbox').slideUp(function() {
                // And show the Privacy Policy field ...
                $ctx.find('.brform').slideDown(function() {
                    $ctx.addClass('active');
                });
            });
        });
    };

    var $compat_save_button = $('#compat-save-button');
    var isSubmitAppPage = $('#page > #submit-payment-type').length;

    // Reset selected device buttons and values.
    $('#submit-payment-type h2 a').click(function(e) {
        var $this = $(this);
        var payment = 'free';
        if ($this.parent().attr('id').split('-')[0] === 'paid') {
            payment = 'paid';
        }
        $('#id_payment').val(payment);

        showOrDisableAndroid();

        if ($this.hasClass('disabled') || $compat_save_button.length) {
            return;
        }

        if (isSubmitAppPage) {
            nullifySelections();
        }
    });

    // Handle clicking of form_factors.
    //
    // When responsive is clicked, we check all form factors. This also handles
    // unclicking and all the variations in between.
    $('#submit-payment-type a.choice').on('click', _pd(function() {
        var $this = $(this);
        var free_or_paid = this.id.split('-')[0];
        var $input = $('#id_form_factor');
        var vals = $input.val() || [];
        var val = $this.attr('data-value');
        var selected = $this.toggleClass('selected').hasClass('selected');
        var $responsive = $('#' + free_or_paid + '-responsive');

        // Check or un-check the checkbox.
        $this.find('input').prop('checked', selected);

        function update_vals(vals, selected, val) {
            if (selected) {
                if (vals.indexOf(val) === -1) {
                    vals.push(val);
                }
            } else {
                vals.splice(vals.indexOf(val), 1);
            }
        }

        if (val === '0') {  // Handle responsive option.
            $('.' + free_or_paid + '-choices').each(function(i, el) {
                var $el = $(el);
                $el.toggleClass('selected', selected);
                $el.find('input').prop('checked', selected);
                update_vals(vals, selected, $el.attr('data-value'));
            });
        } else {
            // Handle other options, single item selected.
            update_vals(vals, selected, val);

            // Handle cases where we need to turn on/off responsive button.
            if (!selected && $responsive.hasClass('selected')) {
                // If deselected but responsive is still selected.
                $responsive.removeClass('selected');
                $responsive.find('input').prop('checked', selected);
            } else if (selected && !$responsive.hasClass('selected')) {
                // If selected but responsive is not selected, check others.
                var enable = true;
                $('.' + free_or_paid + '-choices').each(function(i, el) {
                    if (!$(el).hasClass('selected')) {
                        enable = false;
                    }
                });
                if (enable) {
                    $responsive.addClass('selected');
                    $responsive.find('input').prop('checked', selected);
                }
            }
        }

        // Set platform form values.
        setPlatforms();

        // If mobile (form factor id=2) is the only option selected, set the
        // qHD buchet flag.
        var mobile_id = $('#' + free_or_paid + '-mobile').attr('data-value');
        $('#id_has_qhd').prop('checked', (
            vals.length === 1 && vals[0] === mobile_id)).trigger('change');

        $input.val(vals).trigger('change');
        $compat_save_button.removeClass('hidden');

        // Check if we disable the Android option.
        showOrDisableAndroid();

        // Set hosted/packaged tabs based on chosen form factors.
        setTabState();

    }));

    function nullifySelections() {
        $('#submit-payment-type a.choice').removeClass('selected')
            .find('input').removeAttr('checked');
        $('#id_form_factor').val([]);
    }

    function noFormFactorsChosen() {
        var freeTabs = $('.free-choices.selected').length;
        var paidTabs = $('.paid-choices.selected').length;

        return freeTabs === 0 && paidTabs === 0;
    }

    function setPlatforms() {
        // Set the platform form options.
        var $platforms = $('#id_platform');
        var choices = $('.paid-choices.selected').length ? '.paid-choices' : '.free-choices';
        var mobile_id = $('#free-mobile').attr('data-value');
        var tablet_id = $('#free-tablet').attr('data-value');
        var desktop_id = $('#free-desktop').attr('data-value');

        var vals = [];
        $(choices).each(function(i, el) {
            var $el = $(el);
            var selected = $el.hasClass('selected');

            if (selected) {
                var id = $el.attr('data-value');
                if (id === desktop_id) {
                    // Desktop form factor only applies to desktop platform.
                    vals.push('1');  // 1 == Desktop
                } else if (id === mobile_id || id === tablet_id) {
                    // Mobile/Tablet form factor applies to FxOS platform.
                    vals.push('3');  // 3 == FxOS
                }
            }
        });
        // If the Android checkbox is chosen, include that as well.
        if ($('#platform-android').prop('checked')) {
            vals.push('2');  // 2 == Android
        }

        $platforms.val(vals).trigger('change');
    }

    // Condition to show packaged tab...ugly but works.
    function showPackagedTab() {
        // If the Desktop flag is disabled, and you tried to select
        // Desktop... no packaged apps for you.
        if (!$('[data-packaged-platforms~="desktop"]').length &&
            ($('#free-desktop.selected').length || $('#paid-desktop.selected').length)) {
            return false;
        }

        // If Android is checked and 'android-packaged' is disabled, hide packaged tab.
        if ($('#platform-android').prop('checked') &&
            !$('[data-packaged-platforms~="android"]').length) {
            return false;
        }

        return true;
    }

    // In some cases selecting the Android platform doesn't make sense.
    function showOrDisableAndroid() {
        var disable = false;

        // Disable if Desktop is the only form factor chosen.
        var vals = $('#id_form_factor').val();
        var desktop_id = $('#free-desktop').attr('data-value');
        if (vals && vals.length === 1 && vals[0] === desktop_id) {
            disable = true;
        }

        // Disable if the Paid tab is active and 'android-payments' is disabled.
        if (!$('[data-payment-platforms~="android"]').length && $('#id_payment').val() === 'paid') {
            disable = true;
        }

        // Disable if the Packaged tab is active and 'android-packaged' is disabled.
        if (!$('[data-packaged-platforms~="android"]').length && $('#id_app_type').val() === 'packaged') {
            disable = true;
        }

        var $cb = $('#platform-android');
        $cb.prop('disabled', disable);
        $cb.parent().toggleClass('disabled', disable);
        if (disable) {
            $cb.prop('checked', false).trigger('change');
        }
    }

    // Toggle packaged/hosted tab state.
    function setTabState() {
        // If only free-os or paid-os is selected, show packaged.
        if (showPackagedTab()) {
            $('#packaged-tab-header').css('display', 'inline');
        } else {
            $('#packaged-tab-header').hide();
            $('#hosted-tab-header').find('a').click();
        }
    }

    // If the user checks the Android box, disable various options.
    $('#platform-android').change(function() {
        setTabState();
        setPlatforms();
        $compat_save_button.removeClass('hidden');
    });

    z.body.on('tabs-changed', function(e, tab) {
        if (tab.id == 'packaged-tab-header') {
            $('.learn-mdn.active').removeClass('active');
            $('.learn-mdn.packaged').addClass('active');
            $('#id_app_type').val('packaged');
        } else if (tab.id == 'hosted-tab-header') {
            $('.learn-mdn.active').removeClass('active');
            $('.learn-mdn.hosted').addClass('active');
            $('#id_app_type').val('hosted');
        }
        showOrDisableAndroid();
    });

    // Deselect all checkboxes once tabs have been setup.
    if (isSubmitAppPage) {
        $('.tabbable').bind('tabs-setup', nullifySelections);
    } else {
        // On page load, update the big form factor buttons with the values in
        // the form.
        var free_or_paid = $('#id_payment').val();
        $('#id_form_factor :selected').each(function(i, el) {
            $('#' + free_or_paid + '-' + $(el).text())
                .addClass('selected').find('input').prop('checked', true);
        });
        // If all 3 form factors are chosen, also select responsive.
        if ($('#id_form_factor :selected').length === 3) {
            $('#' + free_or_paid + '-responsive').addClass('selected')
                .find('input').prop('checked', true);
        }
        showOrDisableAndroid();
    }

})(typeof exports === 'undefined' ? (this.submit_details = {}) : exports);


$(document).ready(function() {

    // Anonymous users can view the Developer Agreement page,
    // and then we prompt for log in.
    if (z.anonymous && $('#submit-terms').length) {
        var $login = $('.overlay.login');
        $login.addClass('show');
        $('#submit-terms form').on('click', 'button', _pd(function() {
            $login.addClass('show');
        }));
    }

    // Icon previews.
    imageStatus.start(true, false);
    $('#submit-media').on('click', function(){
        imageStatus.cancel();
    });

    if (document.getElementById('submit-details')) {
        //submit_details.general();
        //submit_details.privacy();
        initCatFields();
        initCharCount();
        initSubmit();
        initTruncateSummary();
    }
    submit_details.houdini();
});
