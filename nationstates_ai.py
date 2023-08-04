import xml.etree.ElementTree as ElementTree
import logging
import time
import aiohttp
import asyncio
import aiosqlite
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    filename="logs.log",
    filemode="a",
    format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    level=logging.DEBUG,
)

class Option:
    __slots__ = "id", "text"

    def __init__(self, option_id: int, text: str):
        self.id = option_id
        self.text = text

class Issue:
    __slots__ = "id", "title", "text", "options"

    def __init__(self, issue_id: int, title: str, text: str, options: list):
        self.id = issue_id
        self.title = title
        self.text = text
        self.options = options

async def manage_ratelimit(response: aiohttp.ClientResponse):
    if int(response.headers["RateLimit-Remaining"]) < 10:
        sleep_time = int(response.headers["RateLimit-Reset"])
        logging.info(f"Pausing server for {sleep_time} seconds to avoid rate-limits.")
        print(f"Pausing server for {sleep_time} seconds to avoid rate-limits.")
        time.sleep(sleep_time)
        logging.info(
            f"Resumed server after sleeping for {sleep_time} seconds to avoid rate-limits."
        )
        print(f"Resumed server after sleeping for {sleep_time} seconds to avoid rate-limits.")

async def parse_issue(issue_text):
    issue_text = ElementTree.fromstring(issue_text)
    issue_list = []
    for issue in issue_text[0]:
        issue_id = int(issue.attrib["id"])
        option_list = []
        for stuff in issue:
            if stuff.tag == "TITLE":
                title = stuff.text
            elif stuff.tag == "TEXT":
                issue_stuff = stuff.text
            elif stuff.tag == "OPTION":
                option_list.append(
                    Option(option_id=int(stuff.attrib["id"]), text=stuff.text)
                )
        try:
            issue_list.append(
                Issue(
                    issue_id=issue_id,
                    title=title,
                    text=issue_stuff,
                    options=option_list,
                )
            )
        except NameError:
            pass
    return issue_list

async def huggingface_query(payload, url, session: aiohttp.ClientSession):
    while True:
        session = aiohttp.ClientSession(headers=session.headers)
        async with session:
            async with session.post(url, json=payload) as response:
                print("Payload sent to model:", payload) 
                response = await response.json()
                try:
                    testing_dict = response["answer"]
                    del testing_dict
                    return response
                except KeyError:
                    print(response)
                    print("AI is offline, retrying in 30 seconds...")
                    logging.error("AI is offline, retrying in 30 seconds...")
                    await asyncio.sleep(30)

async def get_issues(nation, ns_session):
    url = f"https://www.nationstates.net/cgi-bin/api.cgi"
    params = {"nation": nation, "q": "issues"}
    ns_session = aiohttp.ClientSession(headers=ns_session.headers)
    async with ns_session:
        async with ns_session.get(url, params=params) as response:
            await manage_ratelimit(response)
            ns_session.headers.add("X-pin", response.headers["X-pin"])
            response = await response.text()
    with open("issues.txt", "a", encoding="utf-8") as myfile:
        myfile.write(response)
    logging.info(response)
    issue_list = await parse_issue(response)
    for issue in issue_list:
        logging.info(format_issue(issue))
        print(f"Issue id {issue.id}: {format_issue(issue)}")
        with open("issues.txt", "a", encoding="utf-8") as myfile:
            myfile.write(format_issue(issue))
    return [issue_list, ns_session]

def get_issue_results(issue_id):
    url = f"http://www.mwq.dds.nl/ns/results/{issue_id}.html"
    response = requests.get(url)
    soup = BeautifulSoup(response.content, "html.parser")
    table = soup.find("table")
    if table:
        return table.get_text()
    return ""

def format_issue(ns_issue: Issue):
    formatted_issue = f"""{ns_issue.title}

The Issue

{ns_issue.text}

The Debate"""
    index = 1
    for option in ns_issue.options:
        formatted_issue += f"""\n\n{index}. {option.text}"""
        index += 1
    return formatted_issue

def format_question(ns_issue: Issue, prompt: str):
    number_string = ""
    for number in range(1, len(ns_issue.options)):
        number_string += f" {number},"
    number_string += f" or {len(ns_issue.options)}"
    question = (
        f"{prompt}{number_string}? Only input an integer. Other responses will not "
        f"be accepted."
    )
    return question

async def execute_issues(
    nation: str,
    issues: list,
    hf_url: str,
    prompt: str,
    huggingface_session: aiohttp.ClientSession,
    ns_session: aiohttp.ClientSession,
):
    logging.info(f"Executing {len(issues)} issues...")
    execute = []
    for issue in issues:
        logging.info("Contacting AI...")
        print("Contacting AI...")
        selected_option = await huggingface_query(
            {
                "inputs": {
                    "question": format_question(issue, prompt),
                    "context": format_issue(issue),
                    "issue_results": get_issue_results(issue.id),
                },
                "wait_for_model": True
            },
            hf_url,
            huggingface_session,
        )
        print(str(selected_option))
        selected_option = selected_option["answer"]
        logging.info(f"Response: {selected_option}")
        if "The Debate" in selected_option:
            selected_option = selected_option[12:]
        try:
            selected_option = int(selected_option.strip())
            print(selected_option)
            logging.info(selected_option)
            selected_option = issue.options[selected_option - 1].id
            logging.info(f"Final option ID: {selected_option}")
        except ValueError:
            selected_option = selected_option.strip()
            logging.error(
                f"Response was not an integer, searching for response in options..."
            )
            counter = 0
            for option in issue.options:
                counter += 1
                if selected_option in option.text:
                    selected_option = option.id
                    print(f"Found response in option number {counter}")
                    break
        logging.info(f"Executing issue...")
        issue_execution_url = f"https://www.nationstates.net/cgi-bin/api.cgi"
        params = {
            "nation": nation,
            "c": "issue",
            "issue": issue.id,
            "option": selected_option,
        }
        ns_session = aiohttp.ClientSession(headers=ns_session.headers)
        async with ns_session.get(issue_execution_url, params=params) as issue_response:
            if issue_response.status == 200:
                logging.info(f"Executed issue.")
            else:
                logging.info(
                    f"Issue execution failed with error code {issue_response.status}"
                )
                print(f"Issue execution failed with error code {issue_response.status}")
                await manage_ratelimit(issue_response)
                return [execute, aiohttp.ClientSession(headers=ns_session.headers)]
            await manage_ratelimit(issue_response)
            issue_response = await issue_response.text()
        execute.append(issue_response)
        with open("issue_results.txt", "a", encoding="utf-8") as myfile:
            myfile.write(issue_response)
    return [execute, aiohttp.ClientSession(headers=ns_session.headers)]

async def time_to_next_issue(nation: str, ns_session: aiohttp.ClientSession):
    url = "https://www.nationstates.net/cgi-bin/api.cgi"
    params = {"nation": nation, "q": "nextissuetime"}
    async with ns_session:
        async with ns_session.get(url, params=params) as response:
            response = await response.text()
            timestamp = int(ElementTree.fromstring(response)[0].text)
            next_issue_time = timestamp - time.time() + 10
    con = await aiosqlite.connect("nationstates_ai.db")
    cursor = await con.execute(
        "SELECT name FROM sqlite_master WHERE name='next_issue_time'"
    )
    table = await cursor.fetchone()
    if table is None:
        await con.execute("CREATE TABLE next_issue_time(nation, timestamp)")
    data = (nation, timestamp)
    await con.execute("DELETE FROM next_issue_time WHERE nation = ?", (nation,))
    await con.commit()
    await con.execute("""INSERT INTO next_issue_time VALUES(?, ?)""", data)
    await con.commit()
    await con.close()
    return next_issue_time

async def startup_ratelimit(nation, wait_time):
    print(
        f"""Nation {nation} prepared. 
        Sleeping for {wait_time} seconds before starting to avoid rate limits..."""
    )
    logging.info(
        f"""Nation {nation} prepared. 
        Sleeping for {wait_time} seconds before starting to avoid rate limits..."""
    )
    await asyncio.sleep(wait_time)
    print(
        f"""Nation {nation} has woken up and will start automatically answering issues!"""
    )
    logging.info(
        f"""Nation {nation} has woken up and will start automatically answering issues!"""
    )

async def ns_ai_bot(
    nation, password, headers, hf_url, prompt, user_agent, wait_time: int
):
    await startup_ratelimit(nation, wait_time)
    while True:
        ns_session = aiohttp.ClientSession(
            headers={
                "X-Password": password,
                "User-Agent": user_agent + " Nationstates AI v0.1.2-alpha",
            }
        )
        issues = await get_issues(nation, ns_session)
        new_session = await execute_issues(
            nation,
            issues[0],
            hf_url,
            prompt,
            aiohttp.ClientSession(headers=headers),
            issues[1],
        )
        next_issue_time = await time_to_next_issue(nation, new_session[1])
        logging.info(
            f"Nation {nation} sleeping {next_issue_time} seconds until next issue..."
        )
        print(f"Nation {nation} sleeping {next_issue_time} seconds until next issue...")
        await asyncio.sleep(next_issue_time)
