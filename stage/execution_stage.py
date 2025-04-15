# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""The Execution Stage class for measuring code coverage and capture run-time
crashes of the fuzz targets. This stage will run the fuzz target with OSS-Fuzz
infra and report its code coverage and crashes."""
import os

from experiment import builder_runner as builder_runner_lib
from experiment import evaluator as evaluator_lib
from experiment import oss_fuzz_checkout
from experiment.evaluator import Evaluator
from results import BuildResult, Result, RunResult
from stage.base_stage import BaseStage


class ExecutionStage(BaseStage):
  """Executes fuzz targets and build scripts. This stage takes a fuzz target
  and its build script, runs them locally or on the cloud with OSS-Fuzz infra,
  and outputs code coverage report and run-time crash information for later
  stages to analyze and improve on. It uses OSS-Fuzz infra to perform these
  tasks."""

  def execute(self, result_history: list[Result]) -> Result:
    """Executes the fuzz target and build script in the latest result."""
    last_result = result_history[-1]
    benchmark = last_result.benchmark
    if self.args.cloud_experiment_name:
      builder_runner = builder_runner_lib.CloudBuilderRunner(
          benchmark=benchmark,
          work_dirs=last_result.work_dirs,
          run_timeout=self.args.run_timeout,
          experiment_name=self.args.cloud_experiment_name,
          experiment_bucket=self.args.cloud_experiment_bucket,
      )
    else:
      builder_runner = builder_runner_lib.BuilderRunner(
          benchmark=benchmark,
          work_dirs=last_result.work_dirs,
          run_timeout=self.args.run_timeout,
      )

    evaluator = Evaluator(builder_runner, benchmark, last_result.work_dirs)
    generated_target_name = os.path.basename(benchmark.target_path)
    generated_oss_fuzz_project = f'{benchmark.id}-{last_result.trial}'
    generated_oss_fuzz_project = oss_fuzz_checkout.rectify_docker_tag(
        generated_oss_fuzz_project)

    fuzz_target_path = os.path.join(last_result.work_dirs.fuzz_targets,
                                    f'{last_result.trial:02d}.fuzz_target')
    build_script_path = os.path.join(last_result.work_dirs.fuzz_targets,
                                     f'{last_result.trial:02d}.build_script')
    evaluator.create_ossfuzz_project(generated_oss_fuzz_project,
                                     fuzz_target_path, build_script_path)

    status_path = os.path.join(last_result.work_dirs.status,
                               f'{last_result.trial:02}')
    os.makedirs(status_path, exist_ok=True)

    # Try building and running the new target.

    # TODO: Log build failure.
    # TODO: Log run success/failure.

    # 1. Evaluating generated driver.
    if not isinstance(last_result, BuildResult):
      self.logger.error('RunResult must follow a BuildResult')
      raise TypeError

    try:
      _, run_result = evaluator.builder_runner.build_and_run(
          generated_oss_fuzz_project,
          fuzz_target_path,
          0,
          benchmark.language,
          cloud_build_tags=[
              str(last_result.trial),
              'Execution',
              'ofg',
              # TODO(dongge): Tag function name, compatible with tag format.
              last_result.benchmark.project,
          ],
          trial=last_result.trial)
      if not run_result:
        raise Exception('No RunResult received from build_and_run')
      if run_result.coverage_summary is None or run_result.coverage is None:
        self.logger.warning('No cov info in run result of %s',
                            generated_oss_fuzz_project)
        raise Exception(f'No Coverage or Coverage Summary in {run_result}')

      if run_result.coverage_summary:
        total_lines = evaluator_lib.compute_total_lines_without_fuzz_targets(
            run_result.coverage_summary, generated_target_name)
      else:
        total_lines = 0

      if run_result.total_pcs:
        coverage_percent = run_result.cov_pcs / run_result.total_pcs
        self.logger.info('coverage percent == %s in %s.', coverage_percent,
                         generated_oss_fuzz_project)
      else:
        self.logger.warning('total_pcs == 0 in %s.', generated_oss_fuzz_project)
        coverage_percent = 0.0

      existing_textcov = evaluator.load_existing_textcov()
      run_result.coverage.subtract_covered_lines(existing_textcov)

      if total_lines:
        coverage_diff = run_result.coverage.covered_lines / total_lines
        self.logger.info('coverage diff == %s in %s.', coverage_diff,
                         generated_oss_fuzz_project)
      else:
        self.logger.warning('total_lines == 0 in %s',
                            generated_oss_fuzz_project)
        coverage_diff = 0.0

      if run_result.log_path and os.path.isfile(run_result.log_path):
        with open(run_result.log_path, 'r') as f:
          run_log_lines = f.readlines()
          if len(run_log_lines) > 30:
            run_log_lines = (run_log_lines[:20] + [
                f'...({len(run_log_lines) - 30} lines of fuzzing log truncated)'
                '...'
            ] + run_log_lines[-10:])
          run_log_content = ''.join(run_log_lines)
      else:
        run_log_content = ''

      runresult = RunResult(
          benchmark=benchmark,
          trial=last_result.trial,
          work_dirs=last_result.work_dirs,
          fuzz_target_source=last_result.fuzz_target_source,
          build_script_source=last_result.build_script_source,
          author=self,
          compiles=last_result.compiles,
          compile_error=last_result.compile_error,
          compile_log=last_result.compile_log,
          binary_exists=last_result.binary_exists,
          is_function_referenced=last_result.is_function_referenced,
          crashes=run_result.crashes,
          run_error=run_result.crash_info,
          # TODO: This should be the content of log_path.
          run_log=run_log_content,
          coverage_summary=run_result.coverage_summary,
          coverage=coverage_percent,
          line_coverage_diff=coverage_diff,
          reproducer_path=run_result.reproducer_path,
          textcov_diff=run_result.coverage,
          log_path=run_result.log_path,
          corpus_path=run_result.corpus_path,
          coverage_report_path=run_result.coverage_report_path,
          cov_pcs=run_result.cov_pcs,
          total_pcs=run_result.total_pcs,
          chat_history={self.name: run_log_content})
    except Exception as e:
      self.logger.error('Exception %s occurred on %s', e, last_result)
      runresult = RunResult(
          benchmark=benchmark,
          trial=last_result.trial,
          work_dirs=last_result.work_dirs,
          fuzz_target_source=last_result.fuzz_target_source,
          build_script_source=last_result.build_script_source,
          chat_history={self.name: 'Exuection Failed'},
          author=self,
          compiles=last_result.compiles,
          compile_error=last_result.compile_error,
          compile_log=last_result.compile_log,
          binary_exists=last_result.binary_exists,
          is_function_referenced=last_result.is_function_referenced)

    self.logger.write_chat_history(runresult)
    return runresult
