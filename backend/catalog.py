STRUCTURES = {
    "village": {"label": "村庄", "aliases": ["村庄", "village"], "dimension": "overworld"},
    "witch_hut": {"label": "女巫小屋", "aliases": ["女巫小屋", "witch hut", "swamp hut"], "dimension": "overworld"},
    "pillager_outpost": {"label": "掠夺者前哨站", "aliases": ["掠夺者前哨站", "劫掠塔", "pillager outpost"], "dimension": "overworld"},
    "desert_pyramid": {"label": "沙漠神殿", "aliases": ["沙漠神殿", "desert temple", "desert pyramid"], "dimension": "overworld"},
    "jungle_pyramid": {"label": "丛林神庙", "aliases": ["丛林神庙", "jungle temple"], "dimension": "overworld"},
    "igloo": {"label": "雪屋", "aliases": ["雪屋", "igloo"], "dimension": "overworld"},
    "ocean_monument": {"label": "海底神殿", "aliases": ["海底神殿", "ocean monument"], "dimension": "overworld"},
    "woodland_mansion": {"label": "林地府邸", "aliases": ["林地府邸", "woodland mansion"], "dimension": "overworld"},
    "ruined_portal": {"label": "废弃传送门", "aliases": ["废弃传送门", "ruined portal"], "dimension": "overworld"},
    "ancient_city": {"label": "远古城市", "aliases": ["远古城市", "ancient city"], "dimension": "overworld"},
    "trial_chambers": {"label": "试炼密室", "aliases": ["试炼密室", "试炼之地", "试炼地", "审判密室", "trial chambers", "trial chamber"], "dimension": "overworld"},
    "shipwreck": {"label": "沉船", "aliases": ["沉船", "shipwreck"], "dimension": "overworld"},
    "nether_fortress": {"label": "下界要塞", "aliases": ["下界要塞", "nether fortress"], "dimension": "nether"},
    "bastion_remnant": {"label": "堡垒遗迹", "aliases": ["堡垒遗迹", "bastion remnant", "bastion"], "dimension": "nether"},
    "end_city": {"label": "末地城", "aliases": ["末地城", "end city"], "dimension": "end"},
}

BIOMES = {
    "plains": {"label": "平原", "aliases": ["平原", "草原", "plains"]},
    "sunflower_plains": {"label": "向日葵平原", "aliases": ["向日葵平原", "sunflower plains"]},
    "cherry_grove": {"label": "樱花林", "aliases": ["樱花林", "樱花树", "cherry grove"]},
    "swamp": {"label": "沼泽", "aliases": ["沼泽", "swamp"]},
    "mangrove_swamp": {"label": "红树林沼泽", "aliases": ["红树林沼泽", "红树沼泽", "mangrove swamp"]},
    "forest": {"label": "森林", "aliases": ["森林", "forest"]},
    "flower_forest": {"label": "繁花森林", "aliases": ["繁花森林", "花林", "flower forest"]},
    "dark_forest": {"label": "黑森林", "aliases": ["黑森林", "dark forest", "roofed forest"]},
    "desert": {"label": "沙漠", "aliases": ["沙漠", "desert"]},
    "jungle": {"label": "丛林", "aliases": ["丛林", "jungle"]},
    "badlands": {"label": "恶地", "aliases": ["恶地", "badlands"]},
    "savanna": {"label": "热带草原", "aliases": ["热带草原", "金合欢", "金合欢群系", "savanna", "acacia"]},
    "snowy_plains": {"label": "雪原", "aliases": ["雪原", "snowy plains"]},
    "meadow": {"label": "草甸", "aliases": ["草甸", "meadow"]},
    "grove": {"label": "雪林", "aliases": ["雪林", "grove"]},
    "snowy_slopes": {"label": "积雪山坡", "aliases": ["积雪山坡", "snowy slopes"]},
    "jagged_peaks": {"label": "尖峭山峰", "aliases": ["尖峭山峰", "高山", "山峰", "最高的山", "jagged peaks"]},
    "frozen_peaks": {"label": "冰封山峰", "aliases": ["冰封山峰", "frozen peaks"]},
    "stony_peaks": {"label": "裸岩山峰", "aliases": ["裸岩山峰", "stony peaks"]},
    "mushroom_fields": {"label": "蘑菇岛", "aliases": ["蘑菇岛", "mushroom island", "mushroom fields"]},
    "ocean": {"label": "海洋", "aliases": ["海洋", "海", "大海", "ocean"]},
    "warm_ocean": {"label": "暖水海洋", "aliases": ["暖海", "暖水海洋", "warm ocean"]},
    "lukewarm_ocean": {"label": "温水海洋", "aliases": ["温海", "温水海洋", "lukewarm ocean"]},
    "deep_ocean": {"label": "深海", "aliases": ["深海", "deep ocean"]},
    "river": {"label": "河流", "aliases": ["河流", "河", "river"]},
    "beach": {"label": "沙滩", "aliases": ["沙滩", "海滩", "beach"]},
    "sulfur_caves": {"label": "硫磺洞穴", "aliases": ["硫磺洞穴", "sulfur caves"], "experimental": True},
}


def catalog_payload() -> dict:
    return {
        "structures": [{"id": k, **v} for k, v in STRUCTURES.items()],
        "biomes": [{"id": k, **v} for k, v in BIOMES.items()],
    }
