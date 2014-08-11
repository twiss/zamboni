from jingo import register, env
import jinja2
from tower import ugettext as _

import mkt
from mkt.submit.models import AppSubmissionChecklist


def del_by_key(data, delete):
    """Delete a tuple from a list of tuples based on its first item."""
    data = list(data)
    for idx, item in enumerate(data):
        if ((isinstance(item[0], basestring) and item[0] == delete) or
            (isinstance(item[0], (list, tuple)) and item[0] in delete)):
            del data[idx]
    return data


@register.function
def progress(request, addon, step):
    steps = list(mkt.APP_STEPS)

    completed = []

    # TODO: Hide "Developer Account" step if user already read Dev Agreement.
    # if request.user.read_dev_agreement:
    #    steps = del_by_key(steps, 'terms')

    if addon:
        try:
            completed = addon.appsubmissionchecklist.get_completed()
        except AppSubmissionChecklist.DoesNotExist:
            pass

    # We don't yet have a checklist yet if we just read the Dev Agreement.
    if not completed and step and step != 'terms':
        completed = ['terms']

    c = dict(steps=steps, current=step, completed=completed)
    t = env.get_template('submit/helpers/progress.html').render(c)
    return jinja2.Markup(t)
