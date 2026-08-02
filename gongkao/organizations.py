import re

CANONICAL_PATTERNS = [
    ("粉笔", (r"粉笔", r"某笔", r"^FB", r"^fb")),
    ("袁东", (r"袁东", r"^YD$")),
    ("半月谈白鹭", (r"白鹭", r"半月谈")),
    ("千寻申论", (r"千寻", r"qianxun")),
    ("小马哥", (r"小马哥",)),
    ("四海飞扬", (r"四海", r"飞扬")),
    ("申论张嘉庆", (r"张嘉庆",)),
    ("超格李崇立", (r"李崇立",)),
    ("上岸村忠政", (r"上岸村.*忠政", r"忠政")),
    ("人须在事上磨", (r"人[须需].*[事世石]上磨", r"刘大师")),
    ("远志申论", (r"远志",)),
    ("申论小张", (r"申论.*小张", r"小张.*申论", r"爱写申论的小张")),
    ("喵喵公考", (r"喵喵公考",)),
    ("中公教育", (r"中公",)),
    ("华图教育", (r"华图", r"某图")),
    ("公道公考", (r"公道公考",)),
    ("唐棣", (r"唐棣",)),
    ("Kiwi申论", (r"kiwi", r"Kiwi", r"KIWI", r"OK公考", r"ok公考")),
]


def canonicalize_organization(name):
    value = (name or "").strip()
    for canonical, patterns in CANONICAL_PATTERNS:
        if any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns):
            return canonical
    return value
