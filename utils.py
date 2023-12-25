from PIL import ImageFont

STRTOBOOL_DEFAULT_TABLE = {'false': False, 'no': False, '0': False,
                           'true': True, 'yes': True, '1': True,
                           'on': True, 'off': False,
                           'y': True, 'n': False,
                           }


# COPY FROM -> from celery.utils.serialization import strtobool
def strtobool(term, table=None):
    """Convert common terms for true/false to bool.

    Examples (true/false/yes/no/on/off/1/0/y/n).
    """
    if table is None:
        table = STRTOBOOL_DEFAULT_TABLE
    if isinstance(term, str):
        try:
            return table[term.lower()]
        except KeyError:
            raise TypeError(f'Cannot coerce {term!r} to type bool')
    return term


font = ImageFont.truetype("Roboto-Regular.ttf", 12)
