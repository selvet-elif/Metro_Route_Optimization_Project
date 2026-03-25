from collections import defaultdict, deque
import heapq
import json
import math
import os
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GOOGLE_GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"


def _load_google_cache(cache_path: str) -> Dict[str, Any]:
    """
    Loads (or initializes) a JSON cache that prevents re-billing and reduces rate-limit issues.
    Structure: {"geocode": {...}, "directions": {...}}
    """
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("geocode", {})
            data.setdefault("directions", {})
            return data
    except FileNotFoundError:
        pass
    except Exception:
        # Cache failures should never break the simulation.
        pass

    return {"geocode": {}, "directions": {}}


def _save_google_cache(cache_path: str, cache: Dict[str, Any]) -> None:
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        # If caching fails, continue without breaking execution.
        pass


def _get_json(url: str, timeout_s: int = 20) -> Optional[Dict[str, Any]]:
    try:
        req = Request(url, headers={"User-Agent": "MetroRouteOptimization/1.0"})
        with urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def _seconds_to_minutes(seconds: int) -> int:
    # Round up to avoid returning 0 minute durations for very short legs.
    return max(1, int(math.ceil(seconds / 60.0)))


class GoogleMapsClient:
    def __init__(self, api_key: str, cache: Dict[str, Any]):
        self.api_key = api_key
        self.cache = cache

    def geocode(self, address_query: str) -> Optional[Tuple[float, float]]:
        geocode_cache = self.cache.setdefault("geocode", {})
        if address_query in geocode_cache:
            cached = geocode_cache[address_query]
            lat = cached.get("lat")
            lng = cached.get("lng")
            if isinstance(lat, (int, float)) and isinstance(lng, (int, float)):
                return float(lat), float(lng)

        params = {"address": address_query, "key": self.api_key}
        url = GOOGLE_GEOCODE_URL + "?" + urlencode(params)
        data = _get_json(url)
        if not data or data.get("status") != "OK":
            return None

        results = data.get("results") or []
        if not results:
            return None

        loc = results[0].get("geometry", {}).get("location", {})
        lat = loc.get("lat")
        lng = loc.get("lng")
        if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
            return None

        geocode_cache[address_query] = {"lat": float(lat), "lng": float(lng)}
        return float(lat), float(lng)

    def directions_duration_minutes(
        self,
        cache_key: str,
        origin_coords: Tuple[float, float],
        destination_coords: Tuple[float, float],
        transit_mode: str,
        departure_time: str = "now",
    ) -> Optional[int]:
        directions_cache = self.cache.setdefault("directions", {})
        if cache_key in directions_cache:
            cached = directions_cache[cache_key]
            minutes = cached.get("duration_minutes")
            if isinstance(minutes, int):
                return minutes

        origin = f"{origin_coords[0]},{origin_coords[1]}"
        destination = f"{destination_coords[0]},{destination_coords[1]}"
        params = {
            "origin": origin,
            "destination": destination,
            "mode": "transit",
            "transit_mode": transit_mode,
            "departure_time": departure_time,
            "key": self.api_key,
        }
        url = GOOGLE_DIRECTIONS_URL + "?" + urlencode(params)
        data = _get_json(url)
        if not data or data.get("status") != "OK":
            return None

        routes = data.get("routes") or []
        if not routes:
            return None

        legs = routes[0].get("legs") or []
        if not legs:
            return None

        duration = legs[0].get("duration", {})
        seconds = duration.get("value")
        if not isinstance(seconds, int):
            return None

        minutes = _seconds_to_minutes(seconds)
        directions_cache[cache_key] = {"duration_seconds": seconds, "duration_minutes": minutes}
        return minutes

class Istasyon:
    def __init__(self, idx: str, ad: str, hat: str):
        self.idx = idx
        self.ad = ad
        self.hat = hat
        self.komsular: List[Tuple['Istasyon', int]] = []  # (station, duration) tuples

    def komsu_ekle(self, istasyon: 'Istasyon', sure: int):
        self.komsular.append((istasyon, sure))

class MetroAgi:
    def __init__(self):
        self.istasyonlar: Dict[str, Istasyon] = {}
        self.hatlar: Dict[str, List[Istasyon]] = defaultdict(list)
        self.use_google_durations: bool = False

    def istasyon_ekle(self, idx: str, ad: str, hat: str) -> None:
        if id not in self.istasyonlar:
            istasyon = Istasyon(idx, ad, hat)
            self.istasyonlar[idx] = istasyon
            self.hatlar[hat].append(istasyon)

    def baglanti_ekle(self, istasyon1_id: str, istasyon2_id: str, sure: int) -> None:
        istasyon1 = self.istasyonlar[istasyon1_id]
        istasyon2 = self.istasyonlar[istasyon2_id]
        istasyon1.komsu_ekle(istasyon2, sure)
        istasyon2.komsu_ekle(istasyon1, sure)

    def update_edge_weights_with_google(
        self,
        api_key: str,
        cache_path: Optional[str] = None,
        location_hint: str = "Ankara, Turkey",
        transit_modes: Optional[List[str]] = None,
    ) -> int:
        """
        Overwrites each adjacency edge weight with Google Directions 'duration' (minutes).
        Returns number of edges (directed) successfully updated.
        """
        if transit_modes is None:
            transit_modes = ["subway", "train"]

        if cache_path is None:
            # Cache next to this script by default.
            cache_path = os.path.join(os.path.dirname(__file__), "google_maps_cache.json")

        cache = _load_google_cache(cache_path)
        client = GoogleMapsClient(api_key=api_key, cache=cache)

        # 1) Geocode all stations once.
        coords_by_idx: Dict[str, Tuple[float, float]] = {}
        for idx, station in self.istasyonlar.items():
            address_query = f"{station.ad}, {location_hint}"
            coords = client.geocode(address_query)
            if coords is not None:
                coords_by_idx[idx] = coords

        if len(coords_by_idx) < 2:
            _save_google_cache(cache_path, cache)
            return 0

        # 2) Overwrite edge weights.
        updated_edges = 0
        for from_idx, from_station in self.istasyonlar.items():
            if from_idx not in coords_by_idx:
                continue
            for neighbor_i, (to_station, old_sure) in enumerate(from_station.komsular):
                to_idx = to_station.idx
                if to_idx not in coords_by_idx:
                    continue

                minutes = None
                for transit_mode in transit_modes:
                    cache_key = f"{from_idx}|{to_idx}|{transit_mode}"
                    minutes = client.directions_duration_minutes(
                        cache_key=cache_key,
                        origin_coords=coords_by_idx[from_idx],
                        destination_coords=coords_by_idx[to_idx],
                        transit_mode=transit_mode,
                    )
                    if minutes is not None:
                        break

                if minutes is not None and minutes != old_sure:
                    from_station.komsular[neighbor_i] = (to_station, minutes)
                    updated_edges += 1

        _save_google_cache(cache_path, cache)
        return updated_edges
        
        
        
    """Find the route with the fewest transfers using BFS."""
    
    def en_az_aktarma_bul(self, baslangic_id: str, hedef_id: str) -> Optional[List[Istasyon]]:
       
        # Check if start and goal stations exist.
        if baslangic_id not in self.istasyonlar or hedef_id not in self.istasyonlar:
            return None
        
        # Set start and goal stations.
        baslangic = self.istasyonlar[baslangic_id]
        hedef = self.istasyonlar[hedef_id]
        
        # Initialize BFS queue.
        kuyruk = deque([(baslangic, [baslangic])])
        ziyaret_edildi = {baslangic}  # Visited set.
        
        while kuyruk:
            mevcut_istasyon, istasyon_listesi = kuyruk.popleft()   
            
            # Return the path when target station is reached.
            if mevcut_istasyon == hedef:
                return istasyon_listesi
            
            # Mark current station as visited.
            ziyaret_edildi.add(mevcut_istasyon)    

            # Explore neighbors.
            for komsu,_ in mevcut_istasyon.komsular: 
                if komsu not in ziyaret_edildi:
                    kuyruk.append((komsu, istasyon_listesi + [komsu]))
                
        return None
    
            
    """Find the fastest route using A* algorithm."""
    
    def en_hizli_rota_bul(self, baslangic_id: str, hedef_id: str) -> Optional[Tuple[List[Istasyon], int]]:
       
        # Check if start and goal stations exist.
        if baslangic_id not in self.istasyonlar or hedef_id not in self.istasyonlar:
            return None
        
        # Start and goal stations.
        baslangic = self.istasyonlar[baslangic_id]
        hedef = self.istasyonlar[hedef_id]
        
        # Heuristic function.
        # Using a simple transfer-based estimate to keep it admissible.
        def heuristic(istasyon: Istasyon)-> int:
            if self.use_google_durations:
                # With real duration weights, the original line-change heuristic may not be admissible.
                # Using a zero heuristic preserves correctness (A* -> Dijkstra).
                return 0
            if istasyon.hat==hedef.hat:
                return 0  # Heuristic is 0 on same line
            else:
                return 2  # +2 if transfer required
            
            
        # Initialize priority queue.
        # (f, g, start id, current station, station path)
        pq=[(0,0, id(baslangic), baslangic, [baslangic])] 
        
        # Visited stations set
        ziyaret_edildi = set()
        
        while pq:
            
            # Pop station with lowest f(n), i.e. fastest path so far
            _, toplam_sure, _, mevcut_istasyon, istasyon_listesi= heapq.heappop(pq)
            
            # Return path and total time when goal is reached
            if mevcut_istasyon == hedef:
                return (istasyon_listesi, toplam_sure)
            
            # Skip if already visited
            if mevcut_istasyon in ziyaret_edildi:
                continue   
           
            ziyaret_edildi.add(mevcut_istasyon)  # Update visited set
            
            # Traverse neighbors and calculate new path/time
            for komsu, sure in mevcut_istasyon.komsular:
                if komsu not in ziyaret_edildi:
                    
                    yeni_toplam_sure= toplam_sure + sure  # new total time, g(n)
                   
                    yeni_f =  yeni_toplam_sure + heuristic(komsu)   # new cost f(n)
                    
                    # Push new route into priority queue.
                    heapq.heappush(pq, (yeni_f, yeni_toplam_sure, id(komsu), komsu, istasyon_listesi + [komsu] ))
        
        # Return None when no route is found
        return None
                    

    
            
            

# Example Usage
if __name__ == "__main__":
    metro = MetroAgi()
    
    # Add stations
    # Red Line
    metro.istasyon_ekle("K1", "Kızılay", "Kırmızı Hat")
    metro.istasyon_ekle("K2", "Ulus", "Kırmızı Hat")
    metro.istasyon_ekle("K3", "Demetevler", "Kırmızı Hat")
    metro.istasyon_ekle("K4", "OSB", "Kırmızı Hat")
    
    # Blue Line
    metro.istasyon_ekle("M1", "AŞTİ", "Mavi Hat")
    metro.istasyon_ekle("M2", "Kızılay", "Mavi Hat")  # Transfer station
    metro.istasyon_ekle("M3", "Sıhhiye", "Mavi Hat")
    metro.istasyon_ekle("M4", "Gar", "Mavi Hat")
    
    # Orange Line
    metro.istasyon_ekle("T1", "Batıkent", "Turuncu Hat")
    metro.istasyon_ekle("T2", "Demetevler", "Turuncu Hat")  # Transfer station
    metro.istasyon_ekle("T3", "Gar", "Turuncu Hat")  # Transfer station
    metro.istasyon_ekle("T4", "Keçiören", "Turuncu Hat")
    
    # Add connections
    # Red Line connections
    metro.baglanti_ekle("K1", "K2", 4)  # Kızılay -> Ulus
    metro.baglanti_ekle("K2", "K3", 6)  # Ulus -> Demetevler
    metro.baglanti_ekle("K3", "K4", 8)  # Demetevler -> OSB
    
    # Blue Line connections
    metro.baglanti_ekle("M1", "M2", 5)  # AŞTİ -> Kızılay
    metro.baglanti_ekle("M2", "M3", 3)  # Kızılay -> Sıhhiye
    metro.baglanti_ekle("M3", "M4", 4)  # Sıhhiye -> Gar
    
    # Orange Line connections
    metro.baglanti_ekle("T1", "T2", 7)  # Batıkent -> Demetevler
    metro.baglanti_ekle("T2", "T3", 9)  # Demetevler -> Gar
    metro.baglanti_ekle("T3", "T4", 5)  # Gar -> Keçiören
    
    # Transfer edges (same station, different lines)
    metro.baglanti_ekle("K1", "M2", 2)  # Kızılay transfer
    metro.baglanti_ekle("K3", "T2", 3)  # Demetevler transfer
    metro.baglanti_ekle("M4", "T3", 2)  # Gar transfer

    # Optional: overwrite edge weights with Google Maps real durations.
    api_key = os.getenv("GOOGLE_MAPS_API_KEY", "AIzaSyAiwJz5P8Fa-tr9HmRWZxr8FQbCjBfBPgI").strip()
    if api_key:
        try:
            updated_edges = metro.update_edge_weights_with_google(api_key=api_key)
            metro.use_google_durations = updated_edges > 0
            print(f"\n[Google Maps] Updated {updated_edges} directed edges using real Transit durations.")
        except Exception as e:
            metro.use_google_durations = False
            print(f"\n[Google Maps] Integration failed, falling back to hardcoded weights: {e}")
    else:
        print("\n[Google Maps] GOOGLE_MAPS_API_KEY not set; using hardcoded metro edge weights.")
    
    # Test scenarios
    print("\n=== Test Scenarios ===")
    
    # Scenario 1: AŞTİ to OSB
    print("\n1. AŞTİ'den OSB'ye:")
    rota = metro.en_az_aktarma_bul("M1", "K4")
    if rota:
        print("En az aktarmalı rota:", " -> ".join(i.ad for i in rota))
    
    sonuc = metro.en_hizli_rota_bul("M1", "K4")
    if sonuc:
        rota, sure = sonuc
        print(f"En hızlı rota ({sure} dakika):", " -> ".join(i.ad for i in rota))
    
    # Scenario 2: Batıkent to Keçiören
    print("\n2. Batıkent to Keçiören:")
    rota = metro.en_az_aktarma_bul("T1", "T4")
    if rota:
        print("En az aktarmalı rota:", " -> ".join(i.ad for i in rota))
    
    sonuc = metro.en_hizli_rota_bul("T1", "T4")
    if sonuc:
        rota, sure = sonuc
        print(f"En hızlı rota ({sure} dakika):", " -> ".join(i.ad for i in rota))
    
    # Scenario 3: Keçiören to AŞTİ
    print("\n3. Keçiören to AŞTİ:")
    rota = metro.en_az_aktarma_bul("T4", "M1")
    if rota:
        print("En az aktarmalı rota:", " -> ".join(i.ad for i in rota))
    
    sonuc = metro.en_hizli_rota_bul("T4", "M1")
    if sonuc:
        rota, sure = sonuc
        print(f"En hızlı rota ({sure} dakika):", " -> ".join(i.ad for i in rota)) 
        
    # Additional scenarios:
    # Scenario 4: Ulus to Keçiören
    print("\n4. Ulus to Keçiören:")
    rota = metro.en_az_aktarma_bul("K2", "T4")
    if rota:
        print("En az aktarmalı rota:", " -> ".join(i.ad for i in rota))
    
    sonuc = metro.en_hizli_rota_bul("K2", "T4")
    if sonuc:
        rota, sure = sonuc
        print(f"En hızlı rota ({sure} dakika):", " -> ".join(i.ad for i in rota))   
        
        
    # Scenario 5: Sıhhiye to OSB
    print("\n5. Sıhhiye to OSB:")
    rota = metro.en_az_aktarma_bul("M3", "K4")
    if rota:
        print("En az aktarmalı rota:", " -> ".join(i.ad for i in rota))
    
    sonuc = metro.en_hizli_rota_bul("M3", "K4")
    if sonuc:
        rota, sure = sonuc
        print(f"En hızlı rota ({sure} dakika):", " -> ".join(i.ad for i in rota))
        
    
    
