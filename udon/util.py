#
# Copyright (c) 2019 Eric Faurot <eric@faurot.net>
#
# Permission to use, copy, modify, and/or distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
#

class nullcontext:
    def __enter__(self):
        pass
    def __exit__(self, _type, _value, _traceback):
        pass


def find_locale(header: str, default: str = "en"):
    languages = parse_access_language(header)
    for language in languages:
        if language["locale"] in Locales:
            return language["locale"]
    return default


def default_accept_language(header):
    if header:
        languages = parse_access_language(header)
        if languages:
            return languages[0]["locale"]
    return None


def parse_access_language(header):
    """
    Parses the Access-Language header
    ex: "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7,it;q=0.6,th;q=0.5,zh-CN;q=0.4,zh;q=0.3,nb;q=0.2,et;q=0.1,es;q=0.1"
    """
    languages = []
    for level in header.split(","):
        language = {"locale": "",
                    "q": "1"}
        for index, part in enumerate(level.strip().split(";")):
            value = part.split("=")
            if not index:
                language["locale"] = part
            elif len(value) == 2:
                language[value[0]] = value[1]
        languages.append(language)
    return languages
