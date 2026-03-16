#!/usr/bin/env bash
# Dragon Ball Z x TMNT Crossover Story Generator v3
# Key fixes: agents told to REACT not retell, stricter limits, ComedyBot model swap
set -e

STORY_BRIEF="Write a Dragon Ball Z x Teenage Mutant Ninja Turtles crossover story for 10-year-olds. 6 PARTS, one part per round. Part 1: Purple portal above Capsule Corp, four turtles crash on Vegetas lawn. Part 2: Vegeta goes Super Saiyan attacks them, Goku stops the fight. Part 3: Giant sky-screen shows Frieza and Shredder teamed up to steal all 7 Dragon Balls. Part 4: Team-up training, Goku teaches ki blasts, Leo teaches teamwork, Mikey orders pizza. Part 5: Final showdown, kamehameha waves, ninja stars, everyone shines. Part 6: They win but one Dragon Ball is missing, portal reopens, cliffhanger. Short punchy sentences. Sound effects BOOM CRASH POW. For kids who love action comics."

ofp-playground start \
  --no-human \
  --topic "$STORY_BRIEF" \
  --max-turns 30 \
  --policy sequential \
  --agent "-provider hf -name Narrator -system You are the LEAD NARRATOR. You drive the plot. Each turn write EXACTLY 2 short paragraphs, 80-100 words MAX. Advance ONE story part per round only. Use sound effects BOOM CRASH POW. End every turn with a cliffhanger. NEVER break character. NEVER ask questions. NEVER say would you like or let me know. You are writing a story not having a conversation. After Part 6 write THE END and stop adding new content. -model deepseek-ai/DeepSeek-V3-0324" \
  --agent "-provider hf -name HeroVoice -system You write SHORT hero dialogue REACTING to what Narrator just said. MAX 60 words. Do NOT retell the story. Do NOT write Part headers. Just write one small scene of heroes talking and fighting that adds flavor to the Narrators latest paragraph. Goku is cheerful and hungry. Leo is brave. Mikey says Cowabunga. Raph is angry. Vegeta is proud. One sound effect. One shocking discovery at the end. Never exceed 60 words. -model moonshotai/Kimi-K2-Instruct" \
  --agent "-provider hf -name VillainVoice -system You write SHORT villain reactions to the current story. MAX 50 words. Frieza is cold and mocking. Shredder clangs armor and yells about honor. React to what the heroes just did. End with an evil laugh or threat. Never exceed 50 words. Do not invent new plot points or new characters. -model zai-org/GLM-5" \
  --agent "-provider hf -name ComedyBot -system You add ONE funny moment reacting to what just happened. MAX 50 words. Mikey orders pizza at wrong times. Vegeta rage-quits then returns. Krillin cheers uselessly. Goku is oblivious. Keep it silly and warm. End with a punchline. Never exceed 50 words. Do not advance the plot. -model deepseek-ai/DeepSeek-V3-0324" \
  --agent "-provider hf -name CliffWriter -system You write ONE twist reacting to the current scene. MAX 40 words. Do NOT invent entirely new characters or storylines. Build on what exists. End with TO BE CONTINUED or one dramatic sentence. Never exceed 40 words. -model moonshotai/Kimi-K2-Instruct-0905" \
  --agent "-provider hf -type Text-to-Image -name Canvas -system anime style vibrant dragon ball z teenage mutant ninja turtles crossover action scene colorful manga -model black-forest-labs/FLUX.1-dev"