import os
from typing import List

from crewai import Agent, Crew, LLM, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task
from crewai_tools import SerperDevTool

from bot.tools import TravelBudgetCalculatorTool


@CrewBase
class Bot:
    """Travel planner crew."""

    agents: List[BaseAgent]
    tasks: List[Task]
    HARD_MAX_RPM = 30

    @staticmethod
    def _require_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise ValueError(f"Missing required environment variable: {name}")
        return value

    def _llm(self) -> LLM:
        model = self._require_env("MODEL")
        api_key = self._require_env("GROQ_API_KEY")
        if "prompt-guard" in model.lower():
            raise ValueError("MODEL is set to a Prompt-Guard classifier. Use a Groq generative model with tool-calling (example: groq/llama-3.3-70b-versatile).")
        return LLM(model=model, api_key=api_key, temperature=0.1, max_tokens=int(os.getenv("LLM_MAX_TOKENS", "700")))

    def _max_rpm(self) -> int:
        configured = int(os.getenv("LLM_RPM_LIMIT", str(self.HARD_MAX_RPM)))
        return min(configured, self.HARD_MAX_RPM)

    @agent
    def destination_researcher(self) -> Agent:
        self._require_env("SERPER_API_KEY")
        return Agent(
            config=self.agents_config["destination_researcher"],  # type: ignore[index]
            llm=self._llm(),
            tools=[SerperDevTool()],
            max_iter=3,
            max_retry_limit=1,
            allow_delegation=False,
            verbose=True,
        )

    @agent
    def budget_planner(self) -> Agent:
        return Agent(
            config=self.agents_config["budget_planner"],  # type: ignore[index]
            llm=self._llm(),
            tools=[TravelBudgetCalculatorTool()],
            max_iter=3,
            max_retry_limit=1,
            allow_delegation=False,
            verbose=True,
        )

    @agent
    def itinerary_designer(self) -> Agent:
        return Agent(
            config=self.agents_config["itinerary_designer"],  # type: ignore[index]
            llm=self._llm(),
            max_iter=3,
            max_retry_limit=1,
            allow_delegation=False,
            verbose=True,
        )

    @agent
    def validation_agent(self) -> Agent:
        return Agent(
            config=self.agents_config["validation_agent"],  # type: ignore[index]
            llm=self._llm(),
            max_iter=3,
            max_retry_limit=1,
            allow_delegation=False,
            verbose=True,
        )

    @task
    def destination_research_task(self) -> Task:
        return Task(config=self.tasks_config["destination_research_task"])  # type: ignore[index]

    @task
    def budget_planner_task(self) -> Task:
        return Task(config=self.tasks_config["budget_planner_task"])  # type: ignore[index]

    @task
    def itinerary_designer_task(self) -> Task:
        return Task(config=self.tasks_config["itinerary_designer_task"])  # type: ignore[index]

    @task
    def validation_task(self) -> Task:
        return Task(
            config=self.tasks_config["validation_task"],  # type: ignore[index]
            output_file="output.md",
        )

    @crew
    def crew(self) -> Crew:
        """Creates the travel planner crew."""

        destination_task = self.destination_research_task()
        budget_task = self.budget_planner_task()
        itinerary_task = self.itinerary_designer_task()
        validation_task = self.validation_task()

        budget_task.context = [destination_task]
        itinerary_task.context = [destination_task, budget_task]
        validation_task.context = [destination_task, budget_task, itinerary_task]

        return Crew(
            agents=[
                self.destination_researcher(),
                self.budget_planner(),
                self.itinerary_designer(),
                self.validation_agent(),
            ],
            tasks=[destination_task, budget_task, itinerary_task, validation_task],
            process=Process.sequential,
            max_rpm=self._max_rpm(),
            verbose=True,
            output_log_file="logs/execution.log",
        )
