import os
import json
import requests
from langchain.prompts import PromptTemplate
from langchain.chat_models import ChatOpenAI
from langchain.schema import StrOutputParser
from langchain.chains.openai_functions import create_structured_output_runnable
from AI_logic.rule_base.rules_db_conn import query_rule
from AI_logic.airtable import get_record, upsert_record
from dotenv import load_dotenv, find_dotenv
from pushbullet import Pushbullet
from tenacity import retry, stop_after_attempt, wait_fixed
from langchain.pydantic_v1 import BaseModel, Field

# api keys import
load_dotenv(find_dotenv())
language = os.environ['LANGUAGE']
city = os.environ['CITY']
notifications_hook = os.getenv('NOTIFICATIONS_HOOK')

current_dir = os.path.dirname(os.path.realpath(__file__))
# import prompt files
with open(f'{current_dir}/prompts/analyzer.prompt', 'r') as file:
    prompt_template = file.read()
analyzer_prompt = PromptTemplate.from_template(prompt_template)

with open(f'{current_dir}/prompts/commander_step1.prompt', 'r') as file:
    prompt_template = file.read()
commander_step1_prompt = PromptTemplate.from_template(prompt_template)

with open(f'{current_dir}/prompts/commander_step2.prompt', 'r') as file:
    prompt_template = file.read()
commander_step2_prompt = PromptTemplate.from_template(prompt_template)

with open(f'{current_dir}/prompts/writer.prompt', 'r') as file:
    prompt_template = file.read()
writer_prompt = PromptTemplate.from_template(prompt_template)

pushbullet_key = os.getenv('PUSHBULLET_API_KEY')
if pushbullet_key:
    pushbullet = Pushbullet(pushbullet_key)


class AnalyzerOutput(BaseModel):
    summary: str = Field(
        ...,
        description='If in step 1, it should look like: "We are on step 1.\n'
                    'Bond. Important information I know about her (x/3): some info, another info ... .\n'
                    'Image of unavailable guy (x/1): tools used and context.\n'
                    'Fun stries (x/1): what stories Conversator have told.".'
                    'If in step 2, it should look like: "We are on step 2.\n'
                    'Provide here informations about if non-obligatory meeting was proposed, if she was asked '
                    'about number, if comfort was built etc. and some context around that informations."'
                         )
    future_step: str = Field(
        ...,
        description='"step1" if we are currently in step 1 and not all the conditions of that step are completed, '
                    '"step2" if we are currently in step 1 and all the conditions are completed (at least 3 info known,'
                    '1 unavailability tool used, 1 fun story told), "step2" if we are currently in step 2.'
    )
    contact: str = Field(
        ...,
        description='type of contact and contact itself if it was provided by her in last messages. '
                    'For example, "Phone 123456789", "Facebook Name Surname", "Instagram insta_nick". '
                    'If no contact were provided, just leave that field blank.'
    )


class CommanderStep1Output(BaseModel):
    reasoning: str = Field(..., description='Step-by-step reasoning about what abous should be next message and why in 2 sentenses.')
    tags: list = Field(..., description='Choose tags among "Bond", "Attractive guy image", "Storytelling". Make sure you are writing only the tags directly related to your suggestion. Write tags in the array like ["tag1", "tag2"], even if you proposing single tag.')


class CommanderStep2Output(BaseModel):
    reasoning: str = Field(..., description='Step-by-step reasoning about what abous should be next message and why in 2 sentenses.')
    tags: list = Field(..., description='Choose tags among "Suggesting meeting", "Comfort", "Providing meeting details", "Ask for contact". Make sure you are writing only the tags directly related to your suggestion. Write tags in the array like ["tag1", "tag2"], even if you proposing single tag.')


class WriterOutput(BaseModel):
    reasoning: str = Field(..., description='Alright, deep breath. Time to systematically think through your text. Make reasoning about content of your future message.')
    message: list = Field(..., description='["Here\'s your moment. Write the message to girl in {language}.", "Exact that way, your story as two-three separate messages, where next message is continuation of previous", "And hey, grammar nerds unite. Make sure every word is in the right place."]')


Analyzer = ChatOpenAI(model='gpt-4-1106-preview', temperature=0)
Commander = ChatOpenAI(model='gpt-4-1106-preview', temperature=0.4)
Writer = ChatOpenAI(model='gpt-4', temperature=0.7)

analyzer_chain = create_structured_output_runnable(AnalyzerOutput, Analyzer, analyzer_prompt)
writer_chain = writer_prompt | Writer | StrOutputParser()


def commander_chain(future_step):
    if future_step == 'step1':
        return create_structured_output_runnable(CommanderStep1Output, Commander, commander_step1_prompt)
    else:
        return create_structured_output_runnable(CommanderStep2Output, Commander, commander_step2_prompt)


# retry decorator to retry if openai request didn't return
@retry(stop=stop_after_attempt(3), wait=wait_fixed(90))
def invoke_chain(chain, args, module_name=None):
    try:
        output = chain.invoke(args)
        output = json.loads(output)
        print(f'\n{module_name} says:')
        print(json.dumps(output, indent=4, ensure_ascii=False))
        return output
    except Exception as e:
        print(f"Error encountered: \n{str(e)}]n{str(e.args)}\nRetrying...")
        raise e


def respond_to_girl(name_age, messages):
    previous_summary = get_record(name_age)
    analyzer_output = analyzer_chain.invoke({'summary': previous_summary, 'messages': messages})
    print(f'Analyzer says: {analyzer_output}')

    future_step = analyzer_output.future_step
    summary = analyzer_output.summary
    contact = analyzer_output.contact
    if contact:
        if notifications_hook:
            requests.get(notifications_hook, params={'name_age': name_age, 'contact': contact})
        pushbullet.push_note(f"I planned date with {name_age}", contact)
        upsert_record(name_age, not_to_rise=True)
        return

    commander_output = commander_chain(future_step).invoke({'summary': summary, 'messages': messages})

    tags = commander_output.tags
    rules = "\n###\n- ".join([query_rule(tag) for tag in tags])
    writer_output = invoke_chain(writer_chain, {
        'rules': rules,
        'messages': messages,
        'language': language,
        'city': city,
    }, 'Writer')

    messages = writer_output['message']
    # update summary in case of attractive guy image or storytelling
    if 'Attractive guy image' in tags or 'Storytelling' in tags:
        analyzer2_output = analyzer_chain.invoke({'summary': summary, 'messages': f'Conversator: {messages}'})
        print(f'Analyzer says: {analyzer_output}')
        summary = analyzer2_output.summary

    upsert_record(name_age, summary)
    return messages

