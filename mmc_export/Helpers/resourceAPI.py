import asyncio
from collections import namedtuple
from datetime import datetime
from json import loads as parse_json
from pathlib import Path
from re import compile as re_compile
from urllib.parse import urlparse
from zipfile import ZipFile

import tenacity as tn
from aiohttp_client_cache.session import CachedSession

from .structures import Intermediate, Resource
from .utils import delete_github_token, get_github_token, get_hash


class ResourceAPI(object):

    modrinth_search_type: str
    excluded_providers: list[str]
    ignore_CF_flag: bool

    def __init__(self, session: CachedSession, intermediate: Intermediate) -> None:

        self.session = session
        self.intermediate = intermediate

        # Not secure but not plain text either, just a compromise.

        token = b'gAAAAABifAIMNFaSNF8epJIDWIv2nSe3zxARkMmViCa1ZCvtwoRqhuB1LYjjJsAstwTvP4dEOSm6Wj0SRDWr3PPwZz5eEBt_1fU8uIaninakGYFNSarEduD6YfoA-rm28qUQHYpVcuae3lj8sYrs_87P6F4s3gBrYg=='
        key = b'ywE5qRot_nuWfLnbEXXcAPKaW10us3YpWEkDXgm9was='
        from cryptography.fernet import Fernet

        self.session.headers["X-Api-Key"] = Fernet(key).decrypt(token).decode()
        self.session.headers["Content-Type"] = "application/json"
        self.session.headers["Accept"] = "application/json"

        self.github = "https://api.github.com"
        self.modrinth = "https://api.modrinth.com/v2"
        self.curseforge = "https://api.curseforge.com/v1"

        self.cache_directory = Path().home() / ".cache/mmc-export" / "v2"
        self.cache_directory.mkdir(parents=True, exist_ok=True)

        super().__init__()

    def _get_raw_info(self, path: Path) -> tuple[dict, Resource]:

        from pickle import HIGHEST_PROTOCOL
        from pickle import dumps as serialize
        from pickle import loads as deserialize

        cache_file = self.cache_directory / get_hash(path, "xxhash")
        if cache_file.exists():
            data = cache_file.read_bytes()
            meta, resource = deserialize(data)
        else:
            meta = {"name": path.stem,
                    "id": None,
                    "version": "0.0.0"}

            if path.suffix == ".jar":
                with ZipFile(path) as modArchive:
                    filenames = [Path(file).name for file in modArchive.namelist()]
                    if "fabric.mod.json" in filenames:
                        data = modArchive.read("fabric.mod.json")
                        meta = parse_json(data, strict=False)
                    elif "pack.mcmeta" in filenames:
                        data = modArchive.read("pack.mcmeta")
                        json = parse_json(data, strict=False)
                        meta['name'] = json['pack']['description']
                    

            resource = Resource(meta['name'])
            file_data = path.read_bytes()
            resource.file.hash.sha1 = get_hash(file_data, "sha1")
            resource.file.hash.sha256 = get_hash(file_data, "sha256")
            resource.file.hash.sha512 = get_hash(file_data, "sha512")
            resource.file.hash.murmur2 = get_hash(file_data, "murmur2")

            data = serialize((meta, resource), HIGHEST_PROTOCOL)
            cache_file.write_bytes(data)

        resource.file.path = path
        resource.file.name = path.name
        resource.file.relativePath = path.parent.name

        return meta, resource
    
    @tn.retry(stop=tn.stop.stop_after_attempt(5), wait=tn.wait.wait_fixed(1))
    async def _get_github(self, meta: dict, resource: Resource) -> None:

        if "contact" not in meta or "GitHub" in self.excluded_providers: return
        
        for link in meta['contact'].values():
            parsed_link = urlparse(link)

            if parsed_link.netloc == "github.com":
                owner, repo = parsed_link.path[1:].split('/')[:2]
                repo = repo.removesuffix(".git")
                resource.links.append(f"https://github.com/{owner}/{repo}")
                break
        else: return

        async with self.session.get(f"https://api.github.com/repos/{owner}/{repo}/releases") as response:
            if response.status != 200 and response.status != 504: return

            for release in await response.json():
                for asset in release['assets']:
                    if asset['name'] == resource.file.name:
                        url = asset['browser_download_url']
                        author = release['author']['login']
                        break
                else: continue
                break
            else: return

            resource.providers['Other'] = Resource.Provider(
                ID     = None,
                fileID = None,
                url    = url,
                slug   = meta['id'],
                author = author)


class ResourceAPI_Batched(ResourceAPI):

    def __init__(self, session: CachedSession, intermediate: Intermediate) -> None:

        self.queue: list[tuple[dict, Resource]] = list()

        super().__init__(session, intermediate)

    def queue_resource(self, path: Path) -> None:

        meta, resource = self._get_raw_info(path)
        self.queue.append((meta, resource))

    async def gather(self) -> list[Resource]:

        futures = (
            self._get_batched_curseforge(),
            self._get_batched_modrinth(),
            self._get_batched_github()
        )

        await asyncio.gather(*futures)
        resources = [resource for _, resource in self.queue]
        return resources

    @tn.retry(stop=tn.stop.stop_after_attempt(5), wait=tn.wait.wait_fixed(1))
    async def _get_batched_curseforge(self) -> None:

        if "CurseForge" in self.excluded_providers: return

        payload = {"fingerprints":[resource.file.hash.murmur2 for _, resource in self.queue]}
        async with self.session.post(f"{self.curseforge}/fingerprints", json=payload) as response:
            if response.status != 200 and response.status != 504: return
            if matches := (await response.json())['data']['exactMatches']:
                versions = {str(version['file']['fileFingerprint']): version for version in matches}
            else: return

        payload = {"modIds": [version['id'] for version in versions.values()]}
        async with self.session.post(f"{self.curseforge}/mods", json=payload) as response:
            if response.status != 200 and response.status != 504: return
            if addons_array := (await response.json())['data']:
                addons = {addon['id']: addon for addon in addons_array}
            else: return

        for _, resource in self.queue:
            if version := versions.get(resource.file.hash.murmur2):
                if addon := addons.get(version['id']):

                    resource.name = addon['name']
                    resource.links.append(addon['links']['websiteUrl'])
                    if srcUrl := addon['links']['sourceUrl']:
                        resource.links.append(srcUrl)
                    if not self.ignore_CF_flag and not addon['allowModDistribution']: continue

                    resource.providers['CurseForge'] = Resource.Provider(
                        ID     = addon['id'],
                        fileID = version['file']['id'],
                        url    = version['file']['downloadUrl'],
                        slug   = addon['slug'],
                        author = addon['authors'][0]['name'])

    @tn.retry(stop=tn.stop.stop_after_attempt(5), wait=tn.wait.wait_fixed(1))
    async def _get_batched_modrinth(self) -> None:

        if "Modrinth" in self.excluded_providers: return
        search_queue: list[tuple[dict, Resource]] = list()

        payload = {"algorithm": "sha1", "hashes": [resource.file.hash.sha1 for _, resource in self.queue]}
        async with self.session.post(f"{self.modrinth}/version_files", json=payload) as response:
            if response.status != 200 and response.status != 504 and response.status != 423: return
            versions = {v[1]['files'][0]['hashes']["sha1"]: v[1] for v in await response.json()}

            for meta, resource in self.queue:
                if version := versions.get(resource.file.hash.sha1):

                    resource.providers['Modrinth'] = Resource.Provider(
                    ID     = version['project_id'],
                    fileID = version['id'],
                    url    = version['files'][0]['url'],
                    slug   = meta['id'])
                else: search_queue.append((meta, resource))

        if self.modrinth_search_type != "exact": await self._get_batched_modrinth_loose(search_queue)

    @tn.retry(stop=tn.stop.stop_after_attempt(5), wait=tn.wait.wait_fixed(1))
    async def _get_batched_modrinth_loose(self, search_queue: list[tuple[dict, Resource]]) -> None:

        version_ids: list[str] = list()
        
        @tn.retry(stop=tn.stop.stop_after_attempt(5), wait=tn.wait.wait_incrementing(1, 15, 60))
        async def get_project_id(meta: dict, resource: Resource) -> str | None:
            if self.modrinth_search_type == "loose":      
                async with self.session.get(f"{self.modrinth}/search?query={resource.name}") as response: 
                    if response.status != 200 and response.status != 504 and response.status != 423: return
                    if hits := (await response.json())['hits']: return hits[0]['project_id']
            return meta['id']

        futures = (get_project_id(meta, resource) for meta, resource in search_queue)
        project_ids = await asyncio.gather(*futures)
        if not project_ids: return

        l2s = lambda l: "[{}]".format(",".join(map('"{}"'.format, l))) # list to string convesion
        async with self.session.get(f"{self.modrinth}/projects?ids={l2s(project_ids)}") as response:
            if response.status != 200 and response.status != 504 and response.status != 423: return
            for project in await response.json(): version_ids.extend(project['versions'])

        if not version_ids: return

        async with self.session.get(f"{self.modrinth}/versions?ids={l2s(version_ids)}") as response:
            if response.status != 200 and response.status != 504 and response.status != 423: return
            versions = await response.json()
            for meta, resource in search_queue:
                for version in versions:

                    if meta['version'] in version['version_number'] \
                        and self.intermediate.minecraft_version in version['game_versions'] \
                        and self.intermediate.modloader.type in version['loaders']:

                        resource.providers['Modrinth'] = Resource.Provider(
                            ID     = version['project_id'],
                            fileID = version['id'],
                            url    = version['files'][0]['url'],
                            slug   = meta['id'])

                        resource.file.hash.sha1 = version['files'][0]['hashes']['sha1']
                        resource.file.hash.sha512 = version['files'][0]['hashes']['sha512']

                        break

    @tn.retry(stop=tn.stop.stop_after_attempt(5), wait=tn.wait.wait_fixed(1))
    async def _get_batched_github(self) -> None:

        if "GitHub" in self.excluded_providers: return
        
        if not self.session.headers.get('Authorization'):
            if token := get_github_token():
                self.session.headers['Authorization'] = f"Bearer {token}"
            else: 
                futures = [self._get_github(meta, resource) for meta, resource in self.queue]
                await asyncio.gather(*futures)

                async with self.session.disabled():
                    async with self.session.get("https://api.github.com/rate_limit") as response:
                        ratelimit = (await response.json())['resources']['core']
                        time_remaining = datetime.fromtimestamp(float(ratelimit['reset'])).strftime("%H:%M")
                        remaining, limit = ratelimit['remaining'], ratelimit['limit']
                        if ratelimit['remaining'] == 0: 
                            print("You have exceeded the GitHub API rate-limit, only cached results will be used.")
                            print(f"Please sign in with `mmc-export gh-login` or try again at {time_remaining}")
                        else:
                            print(f"{remaining}/{limit} GitHub searches remaining, after that only cached results will be used.")
                            print(f"To get more searches, sign in with `mmc-export gh-login` or wait until {time_remaining} to get {limit} searches again.")
                    return

        Repository = namedtuple('Repository', ['name', 'owner', 'alias'])
        repositories: list[Repository] = list()
        pattern = re_compile(r"[\W_]+")

        for meta, resource in self.queue:
            if "contact" not in meta: continue
            for link in meta['contact'].values():
                parsed_link = urlparse(link)

                if parsed_link.netloc == "github.com":
                    alias = pattern.sub('', meta['id'])
                    owner, name = parsed_link.path[1:].split('/')[:2]
                    repo = Repository(name.removesuffix(".git"), owner, alias)
                    resource.links.append(f"https://github.com/{repo.owner}/{repo.name}")
                    repositories.append(repo)
                    break
            else: continue

        from gql_query_builder import GqlQuery
        queries: list[str] = list()
        
        for repo in repositories:
            query = GqlQuery() \
                .fields(['...repoReleaseAssets']) \
                .query('repository', alias=repo.alias, input={"name": f'"{repo.name}"', "owner": f'"{repo.owner}"'}) \
                .generate()
            queries.append(query)

        payload = """
        fragment repoReleaseAssets on Repository {
            releases(last: 100) { edges { node {
                releaseAssets(last: 10) { nodes {
                    name
                    downloadUrl
        } } } } } } """ + GqlQuery().operation(queries=queries).generate()

        async with self.session.post(f"{self.github}/graphql", json={"query": payload}) as response:
            if response.status == 401: delete_github_token(); raise tn.TryAgain
            if response.status != 200 and response.status != 504: return
            data = (await response.json())['data']      

            for meta, resource in self.queue:
                if not data.get(alias := pattern.sub('', meta['id']) if meta['id'] else "unknown"): continue
                for release in data.get(alias, {}).get('releases', {}).get('edges', []):
                    for asset in release.get('node', {}).get('releaseAssets', {}).get('nodes', []):
                        if asset['name'] == resource.file.name: url = asset['downloadUrl']; break
                    else: continue
                    break
                else: continue

                resource.providers['Other'] = Resource.Provider(
                    ID     = None,
                    fileID = None,
                    url    = url,
                    slug   = meta['id'])
