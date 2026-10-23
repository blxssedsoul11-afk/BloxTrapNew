
import aiohttp
import asyncio
from typing import Optional, Dict, List
from datetime import datetime, timedelta
import time

class RobloxAPI:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.cache: Dict[str, tuple] = {}
        self.cache_ttl = {
            'user_info': 300,
            'avatar': 300,
            'presence': 10
        }
        self.rate_limit_delay = 0.15
        self.last_request_time = 0
        
    async def create_session(self):
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=15, connect=5)
            connector = aiohttp.TCPConnector(limit=10, limit_per_host=5, force_close=False)
            self.session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector
            )
    
    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()
            await asyncio.sleep(0.1)
    
    def _get_cached(self, cache_key: str, cache_type: str) -> Optional[any]:
        if cache_key in self.cache:
            data, timestamp = self.cache[cache_key]
            ttl = self.cache_ttl.get(cache_type, 60)
            if time.time() - timestamp < ttl:
                return data
            else:
                del self.cache[cache_key]
        return None
    
    def _set_cache(self, cache_key: str, data: any):
        self.cache[cache_key] = (data, time.time())
        
        if len(self.cache) > 1000:
            oldest_key = min(self.cache.keys(), key=lambda k: self.cache[k][1])
            del self.cache[oldest_key]
    
    async def _rate_limit(self):
        current_time = time.time()
        time_since_last = current_time - self.last_request_time
        if time_since_last < self.rate_limit_delay:
            await asyncio.sleep(self.rate_limit_delay - time_since_last)
        self.last_request_time = time.time()
    
    async def _make_request(self, method: str, url: str, **kwargs) -> Optional[Dict]:
        await self.create_session()
        await self._rate_limit()
        
        max_retries = 3
        retry_delay = 1
        
        for attempt in range(max_retries):
            try:
                async with self.session.request(method, url, **kwargs) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status == 429:
                        retry_after = int(response.headers.get('Retry-After', retry_delay * (attempt + 1)))
                        print(f"Rate limited, waiting {retry_after}s")
                        await asyncio.sleep(retry_after)
                        continue
                    elif response.status >= 500:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay * (attempt + 1))
                            continue
                    elif response.status == 400:
                        print(f"Bad request to {url}: {await response.text()}")
                        return None
                    return None
            except asyncio.TimeoutError:
                print(f"Timeout on attempt {attempt + 1} for {url}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                return None
            except aiohttp.ClientError as e:
                print(f"Client error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                return None
            except Exception as e:
                print(f"Request error on attempt {attempt + 1}: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay * (attempt + 1))
                    continue
                return None
        
        return None
    
    async def get_user_info(self, user_id: int) -> Optional[Dict]:
        cache_key = f"user_info_{user_id}"
        cached = self._get_cached(cache_key, 'user_info')
        if cached is not None:
            return cached
        
        url = f"https://users.roblox.com/v1/users/{user_id}"
        data = await self._make_request('GET', url)
        
        if data:
            result = {
                'id': data.get('id'),
                'name': data.get('name'),
                'displayName': data.get('displayName'),
                'description': data.get('description', ''),
                'created': data.get('created', ''),
                'hasVerifiedBadge': data.get('hasVerifiedBadge', False)
            }
            self._set_cache(cache_key, result)
            return result
        return None
    
    async def get_user_avatar_url(self, user_id: int) -> Optional[str]:
        cache_key = f"avatar_{user_id}"
        cached = self._get_cached(cache_key, 'avatar')
        if cached is not None:
            return cached
        
        url = f"https://thumbnails.roblox.com/v1/users/avatar?userIds={user_id}&size=420x420&format=Png&isCircular=false"
        data = await self._make_request('GET', url)
        
        if data and data.get('data') and len(data['data']) > 0:
            avatar_url = data['data'][0].get('imageUrl')
            self._set_cache(cache_key, avatar_url)
            return avatar_url
        return None
    
    async def get_user_presence(self, user_id: int) -> Optional[Dict]:
        cache_key = f"presence_{user_id}"
        cached = self._get_cached(cache_key, 'presence')
        if cached is not None:
            return cached
        
        url = "https://presence.roblox.com/v1/presence/users"
        payload = {"userIds": [user_id]}
        data = await self._make_request('POST', url, json=payload)
        
        if data and data.get('userPresences') and len(data['userPresences']) > 0:
            presence = data['userPresences'][0]
            result = {
                'userPresenceType': presence.get('userPresenceType', 0),
                'lastLocation': presence.get('lastLocation', ''),
                'placeId': presence.get('placeId'),
                'rootPlaceId': presence.get('rootPlaceId'),
                'gameId': presence.get('gameId'),
                'universeId': presence.get('universeId'),
                'userId': presence.get('userId'),
                'lastOnline': presence.get('lastOnline', '')
            }
            self._set_cache(cache_key, result)
            return result
        return None
    
    async def get_ropro_invite(self, universe_id, game_id) -> Optional[str]:
        """Create a RoPro invite link for a specific server (no auth required)."""
        if not universe_id or not game_id:
            return None
        await self.create_session()
        url = f"https://api.ropro.io/createInvite.php?universeid={universe_id}&serverid={game_id}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                text = (await resp.text()).strip()
                if resp.status == 200 and text.startswith("https://ropro.io/invite/"):
                    return text
                print(f"RoPro invite failed ({resp.status}): {text[:200]}")
        except Exception as e:
            print(f"RoPro invite error: {e}")
        return None

    async def get_player_status(self, user_id: int) -> Dict:
        try:
            presence_task = asyncio.create_task(self.get_user_presence(user_id))
            user_info_task = asyncio.create_task(self.get_user_info(user_id))
            
            presence, user_info = await asyncio.gather(presence_task, user_info_task, return_exceptions=True)
            
            # Handle exceptions from gather
            if isinstance(presence, Exception):
                print(f"Error getting presence for {user_id}: {presence}")
                presence = None
            if isinstance(user_info, Exception):
                print(f"Error getting user info for {user_id}: {user_info}")
                user_info = None

            if not presence or not user_info:
                return {
                    'online': False,
                    'status': 'Offline',
                    'user_info': user_info if user_info else {}
                }

            presence_type = presence.get('userPresenceType', 0)

            if presence_type == 2:
                game_location = presence.get('lastLocation', 'Playing')
                return {
                    'online': True,
                    'status': 'Online',
                    'game': game_location,
                    'user_info': user_info,
                    'presence': presence
                }
            else:
                return {
                    'online': False,
                    'status': 'Offline',
                    'user_info': user_info,
                    'presence': presence
                }
        except Exception as e:
            print(f"Unexpected error in get_player_status for {user_id}: {e}")
            return {
                'online': False,
                'status': 'Offline',
                'user_info': {}
            }
    
    async def get_multiple_user_presences(self, user_ids: List[int]) -> Dict[int, Optional[Dict]]:
        if not user_ids:
            return {}
        
        url = "https://presence.roblox.com/v1/presence/users"
        
        results = {}
        batch_size = 50
        
        for i in range(0, len(user_ids), batch_size):
            batch = user_ids[i:i + batch_size]
            
            uncached_ids = []
            for user_id in batch:
                cache_key = f"presence_{user_id}"
                cached = self._get_cached(cache_key, 'presence')
                if cached is not None:
                    results[user_id] = cached
                else:
                    uncached_ids.append(user_id)
            
            if uncached_ids:
                payload = {"userIds": uncached_ids}
                data = await self._make_request('POST', url, json=payload)
                
                if data and data.get('userPresences'):
                    for presence in data['userPresences']:
                        user_id = presence.get('userId')
                        if user_id:
                            result = {
                                'userPresenceType': presence.get('userPresenceType', 0),
                                'lastLocation': presence.get('lastLocation', ''),
                                'placeId': presence.get('placeId'),
                                'rootPlaceId': presence.get('rootPlaceId'),
                                'gameId': presence.get('gameId'),
                                'universeId': presence.get('universeId'),
                                'userId': presence.get('userId'),
                                'lastOnline': presence.get('lastOnline', '')
                            }
                            cache_key = f"presence_{user_id}"
                            self._set_cache(cache_key, result)
                            results[user_id] = result
        
        return results
    
    def clear_cache(self, cache_type: Optional[str] = None):
        if cache_type:
            keys_to_delete = [k for k in self.cache.keys() if k.startswith(cache_type)]
            for key in keys_to_delete:
                del self.cache[key]
        else:
            self.cache.clear()
