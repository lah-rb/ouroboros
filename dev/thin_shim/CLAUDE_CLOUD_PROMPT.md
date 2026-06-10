I'm developing Ouroboros, a flow-driven autonomous coding agent that runs local LLMs. I need you to help me find the gaps between what I know about this project and what the model actually sees when it receives a prompt.
The project includes a thin shim server that lets you stand in for the LLM. You'll receive the same prompts any local model would see — with no other context. Your job is to experience each prompt cold and tell me:
* Is the instruction clear enough to act on without outside knowledge?
* Is the expected output format obvious from the prompt alone?
* Is the provided context sufficient, or are you filling in gaps from having read the codebase?
* What would a model that has never seen this project get wrong?
Start by reading THIN_SHIM.md for setup and the iterative discovery test pattern. Install dependencies and set up the environment as described. Do NOT read other project files beyond what setup requires — I want your unbiased reaction to the prompts as they arrive.
Once the environment is ready, create a mission and begin the discovery loop: launch, hit the first unknown prompt, read it, respond as the model would, report what you saw, then add the response to your classifier and advance to the next prompt.
The code the agent produces matters less than your prompt-by-prompt assessment.

After each cycle completes, before relaunching: check what files changed on disk (find or diff), read the updated mission.json task statuses, and compare the prompt you just saw against earlier prompts of the same type — note any context changes (new files in "Existing Files," different task statuses in the plan, new entries in "Last Cycle"). Report these observations before adding the classifier rule and advancing. The diffs between successive prompts of the same type are where subtle bugs hide.