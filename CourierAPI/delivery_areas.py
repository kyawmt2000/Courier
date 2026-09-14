from pydantic import BaseModel


class DeliveryAreaResponse(BaseModel):
    city: str
    townships: list[str]
    center_lat: float
    center_lng: float
    lat_min: float
    lat_max: float
    lng_min: float
    lng_max: float
    margin: float


ACTIVE_DELIVERY_AREAS: list[DeliveryAreaResponse] = [
    DeliveryAreaResponse(
        city="Yangon",
        townships=[
            "Ahlone",
            "Bahan",
            "Dagon",
            "Kamayut",
            "Kyauktada",
            "Kyeemyindaing",
            "Lanmadaw",
            "Latha",
            "Pabedan",
            "Sanchaung",
            "Botahtaung",
            "Dagon Myothit (East)",
            "Dagon Myothit (North)",
            "Dagon Myothit (Seikkan)",
            "Dagon Myothit (South)",
            "Dawbon",
            "Mingalartaungnyunt",
            "North Okkalapa",
            "Pazundaung",
            "South Okkalapa",
            "Tamwe",
            "Thaketa",
            "Thingangyun",
            "Yankin",
            "Hlaing",
            "Hlaingthaya",
            "Hmawbi",
            "Htantabin",
            "Insein",
            "Mayangone",
            "Mingaladon",
            "Shwepyitha",
            "Cocokyun",
            "Dala",
            "Kawhmu",
            "Kayan",
            "Kungyangon",
            "Kyauktan",
            "Seikkan",
            "Seikkyi Kanaungto",
            "Thanlyin",
            "Thongwa",
            "Twantay",
        ],
        center_lat=16.840900,
        center_lng=96.173500,
        lat_min=15.95,
        lat_max=17.35,
        lng_min=95.65,
        lng_max=96.75,
        margin=0.25,
    ),
    DeliveryAreaResponse(
        city="Taungoo",
        townships=["Taungoo"],
        center_lat=18.942000,
        center_lng=96.434000,
        lat_min=18.70,
        lat_max=19.15,
        lng_min=96.20,
        lng_max=96.70,
        margin=0.20,
    ),
    # DeliveryAreaResponse(
    #     city="Mandalay",
    #     townships=["Aungmyethazan", "Chanayethazan", "Mahaaungmye", "Chanmyathazi", "Pyigyidagun", "Patheingyi"],
    #     center_lat=21.958800,
    #     center_lng=96.089100,
    #     lat_min=21.65,
    #     lat_max=22.35,
    #     lng_min=95.75,
    #     lng_max=96.35,
    #     margin=0.20,
    # ),
    # DeliveryAreaResponse(
    #     city="Naypyidaw",
    #     townships=["Zabuthiri", "Dekkhinathiri", "Pobbathiri", "Ottarathiri", "Zeyathiri", "Pyinmana", "Lewe", "Tatkone"],
    #     center_lat=19.763300,
    #     center_lng=96.078500,
    #     lat_min=19.35,
    #     lat_max=20.10,
    #     lng_min=95.70,
    #     lng_max=96.45,
    #     margin=0.25,
    # ),
    # DeliveryAreaResponse(city="Bago", townships=["Bago"], center_lat=17.335200, center_lng=96.481400, lat_min=17.05, lat_max=17.65, lng_min=96.15, lng_max=96.85, margin=0.20),
    # DeliveryAreaResponse(city="Mawlamyine", townships=["Mawlamyine"], center_lat=16.490500, center_lng=97.628300, lat_min=16.20, lat_max=16.75, lng_min=97.35, lng_max=97.90, margin=0.20),
    # DeliveryAreaResponse(city="Taunggyi", townships=["Taunggyi"], center_lat=20.788800, center_lng=97.035400, lat_min=20.55, lat_max=21.05, lng_min=96.75, lng_max=97.30, margin=0.20),
]


def delivery_townships_by_city() -> dict[str, list[str]]:
    return {area.city: area.townships for area in ACTIVE_DELIVERY_AREAS}


def city_bounds_with_margin() -> dict[str, tuple[tuple[float, float], tuple[float, float], float]]:
    return {
        area.city: ((area.lat_min, area.lat_max), (area.lng_min, area.lng_max), area.margin)
        for area in ACTIVE_DELIVERY_AREAS
    }
