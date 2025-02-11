from typing import *
from datetime import datetime
from pathlib import Path
import re
from tqdm import tqdm
import traceback
from recordclass import asdict
from urllib.error import HTTPError
from urllib.request import urlopen
from xml.etree import ElementTree
import shutil
import os
from contextlib import contextmanager

from seutil import LoggingUtils, IOUtils, BashUtils, TimeUtils, GitHubUtils
from seutil.project import Project

from pts.Environment import Environment
from pts.Macros import Macros
from pts.data.ProjectData import ProjectData
from pts.Utils import Utils


@contextmanager
def change_dir(destination: Path):
    """
    Context manager for changing the current working directory.
    Automatically returns to the original directory after the operation.
    """
    original_dir = Path.cwd()
    try:
        os.chdir(destination)
        yield
    finally:
        os.chdir(original_dir)


class DataCollector:
    logger = LoggingUtils.get_logger(__name__, LoggingUtils.DEBUG if Environment.is_debug else LoggingUtils.INFO)

    def __init__(self):
        self.repos_downloads_dir: Path = Macros.repos_downloads_dir
        self.repos_results_dir: Path = Macros.repos_results_dir
        self.collected_projects_list = []

    def collect_data(self, **options):
        which = Utils.get_option_as_list(options, "which")
        for item in which:
            self.logger.info(f"Collecting data: {item}; options: {options}")
            if item == "eval-data":
                self.collect_eval_data(**options)
            elif item == "test-method-data":
                self.collect_test_method(**options)
            elif item == "method-data-collect":
                project = options.get("project")
                self.collect_project_with_shas(project)
            else:
                raise NotImplementedError

    def collect_project_with_shas(self, project: str):
        from pts.collector.ProjectParser import ProjectParser
        eval_ag_file = Macros.eval_data_dir / "mutated-eval-data" / f"{project}-ag.json"
        eval_ag_data_list = IOUtils.load(eval_ag_file)
        sha_list = set()
        for eval_data in eval_ag_data_list:
            sha_list.add(eval_data["commit"].split('-')[0])

        pp = ProjectParser()
        for revision in sha_list:
            pp.parse_project(project, revision)
            self.collect_test_methods_with_shas(project, revision)

    def collect_test_methods_with_shas(self, project: str, revision: str):
        from collections import defaultdict
        project_result_dir = self.repos_results_dir / project / revision / "collector"
        project_method_file = project_result_dir / "method-data.json"
        method_list = IOUtils.load(project_method_file)
        test_class_2_methods = defaultdict(list)
        for m in method_list:
            if "src/test" in m["path"] or "Test.java" in m["path"]:
                class_name = m["path"].split('/')[-1].split('.java')[0]
                test_class_2_methods[class_name].append(m["id"])

        IOUtils.dump(project_result_dir / "test2methods.json", test_class_2_methods)

    def collect_test_method(self, **options):
        from collections import defaultdict
        projects = Utils.get_option_as_list(options, "projects")
        for project in projects:
            project_result_dir = self.repos_results_dir / project / "collector"
            project_method_file = project_result_dir / "method-data.json"
            method_list = IOUtils.load(project_method_file)
            test_class_2_methods = defaultdict(list)
            for m in method_list:
                if "src/test" in m["path"] or "Test.java" in m["path"]:
                    test_class_2_methods[m["class_name"]].append(m["id"])

            IOUtils.dump(project_result_dir / "test2methods.json", test_class_2_methods)

    def collect_eval_data(self, **options):
        projects = Utils.get_option_as_list(options, "projects")
        shas = Utils.get_option_as_list(options, "shas")
        num_sha = options.get("num_sha", 20)
        proj_dict = {}
        for p, sh in zip(projects, shas):
            proj_dict[p] = sh

        from pts.collector.eval_data_collection import main
        main(proj_dict)

    def download_projects(self, project_list: Dict):
        for project, sha in tqdm(project_list.items(), total=len(project_list)):
            try:
                project_url = self.parse_repo_name(project)

                if not self.check_github_url(project_url):
                    self.logger.warning(f"Project {project} no longer available.")
                    continue

                downloads_dir = self.repos_downloads_dir / project
                results_dir = self.repos_results_dir / project

                shutil.rmtree(results_dir, ignore_errors=True)
                results_dir.mkdir(parents=True, exist_ok=True)

                if not downloads_dir.exists():
                    with change_dir(self.repos_downloads_dir):
                        with TimeUtils.time_limit(300):
                            BashUtils.run(f"git clone {project_url} {project}", expected_return_code=0)
                        if downloads_dir.exists():
                            with change_dir(downloads_dir):
                                BashUtils.run(f"git checkout {sha}", expected_return_code=0)
                        else:
                            self.logger.warning(f"{project} is not downloaded!")
            except KeyboardInterrupt:
                self.logger.warning(f"KeyboardInterrupt")
                break
            except Exception:
                self.logger.warning(f"Collection for project {project_url} failed, error was: {traceback.format_exc()}")

    def collect_projects(self, project_urls_file: Path, skip_collected: bool, beg: int = None, cnt: int = None):
        project_urls = IOUtils.load(project_urls_file, IOUtils.Format.txt).splitlines()
        invalid_project_urls = list()

        if beg is None: beg = 0
        if cnt is None: cnt = len(project_urls)

        project_urls = project_urls[beg:beg + cnt]

        for pi, project_url in enumerate(project_urls):
            self.logger.info(f"Project {beg + pi + 1}/{len(project_urls)}({beg}-{beg + cnt}): {project_url}")

            try:
                user_repo = self.parse_github_url(project_url)
                if user_repo is None:
                    self.logger.warning(f"URL {project_url} is not a valid GitHub repo URL.")
                    invalid_project_urls.append(project_url)
                    continue

                project_name = f"{user_repo[0]}_{user_repo[1]}"

                if skip_collected and self.is_project_collected(project_name, project_url):
                    self.logger.info(f"Project {project_name} already collected.")
                    continue

                if not self.check_github_url(project_url):
                    self.logger.warning(f"Project {project_name} no longer available.")
                    invalid_project_urls.append(project_url)
                    continue

                pom_xml_url = project_url[:-len(".git")] + "/blob/master/pom.xml"
                try:
                    urlopen(pom_xml_url)
                except HTTPError:
                    self.logger.info(f"Project {project_name} does not seem to be a Maven project. Moving to nouse set")
                    continue

                self.collect_project(project_name, project_url)
            except KeyboardInterrupt:
                self.logger.warning(f"KeyboardInterrupt")
                break
            except:
                self.logger.warning(f"Collection for project {project_url} failed, error was: {traceback.format_exc()}")

        IOUtils.dump(Macros.results_dir / "collected-projects.txt", self.collected_projects_list, IOUtils.Format.txt)

    def collect_project(self, project_name: str, project_url: str):
        Environment.require_collector()

        downloads_dir = self.repos_downloads_dir / project_name
        results_dir = self.repos_results_dir / project_name

        shutil.rmtree(results_dir, ignore_errors=True)
        results_dir.mkdir(parents=True, exist_ok=True)

        if not downloads_dir.exists():
            with change_dir(self.repos_downloads_dir):
                with TimeUtils.time_limit(300):
                    BashUtils.run(f"git clone {project_url} {project_name}", expected_return_code=0)

        if not self.check_junit(downloads_dir / "pom.xml"):
            self.logger.info(f"Project {project_name} does not satisfy the dependency requirements.")
            shutil.rmtree(downloads_dir, ignore_errors=True)
            shutil.rmtree(results_dir, ignore_errors=True)
            return

        project_data = ProjectData.create()
        project_data.name = project_name
        project_data.url = project_url

        with change_dir(downloads_dir):
            git_log_out = BashUtils.run(f"git rev-parse HEAD", expected_return_code=0).stdout
            project_data.revision = git_log_out

        project_data_file = results_dir / "project.json"
        IOUtils.dump(project_data_file, asdict(project_data), IOUtils.Format.jsonPretty)

        log_file = results_dir / "collector-log.txt"
        output_dir = results_dir / "collector"

        config = {
            "collect": True,
            "projectDir": str(downloads_dir),
            "projectDataFile": str(project_data_file),
            "logFile": str(log_file),
            "outputDir": str(output_dir),
        }
        config_file = results_dir / "collector-config.json"
        IOUtils.dump(config_file, config, IOUtils.Format.jsonPretty)

        self.logger.info(f"Starting the Java collector. Check log at {log_file} and outputs at {output_dir}")
        rr = BashUtils.run(f"java -jar {Environment.collector_jar} {config_file}", expected_return_code=0)
        if rr.stderr:
            self.logger.warning(f"Stderr of collector:\n{rr.stderr}")

        self.collected_projects_list.append(project_url)

    REQUIREMENTS = {
        "junit": lambda v: int(v) == 4
    }

    @classmethod
    def check_junit(cls, pom_file: Path) -> bool:
        namespaces = {'xmlns': 'http://maven.apache.org/POM/4.0.0'}

        tree = ElementTree.parse(pom_file)
        root = tree.getroot()

        deps = root.findall(".//xmlns:dependency", namespaces=namespaces)
        for d in deps:
            artifact_id = d.find("xmlns:artifactId", namespaces=namespaces).text
            if artifact_id in cls.REQUIREMENTS.keys():
                version = d.find("xmlns:version", namespaces=namespaces).text.split(".")[0]
                if cls.REQUIREMENTS[artifact_id](version):
                    return True
                else:
                    return False
        return False

    RE_GITHUB_URL = re.compile(r"https://github\.com/(?P<user>[^/]+)/(?P<repo>.+?)(\.git)?")

    @classmethod
    def parse_repo_name(cls, project_name):
        github_user_name = project_name.split('_')[0]
        github_project_name = project_name.split('_')[1]

        github_url = f"https://github.com/{github_user_name}/{github_project_name}.git"

        return github_url

    @classmethod
    def parse_github_url(cls, github_url) -> Tuple[str, str]:
        m = cls.RE_GITHUB_URL.fullmatch(github_url)
        if m is None:
            return None
        else:
            return m.group("user"), m.group("repo")

    def is_project_collected(self, project_name, project_url):
        return project_name in self.collected_projects_list or project_url in self.collected_projects_list

    @classmethod
    def check_github_url(cls, github_url):
        try:
            urlopen(github_url)
            return True
        except HTTPError:
            return False

    def get_github_top_repos(self):
        repositories = GitHubUtils.search_repos(q="topic:java language:java", sort="stars", order="desc",
                                                max_num_repos=1000)
        for repo in repositories:
            project = Project()
            project.url = GitHubUtils.ensure_github_api_call(lambda g: repo.clone_url)
            project.data["user"] = GitHubUtils.ensure_github_api_call(lambda g: repo.owner.login)
            project.data["repo"] = GitHubUtils.ensure_github_api_call(lambda g: repo.name)
            project.full_name = f"{project.data['user']}_{project.data['repo']}"
            project.data["branch"] = GitHubUtils.ensure_github_api_call(lambda g: repo.default_branch)
